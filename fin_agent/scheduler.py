import time
import schedule
import threading
import json
import os
import logging
from pathlib import Path
from fin_agent.config import Config
from fin_agent.notification import NotificationManager


import errno
import logging
import platform

logger = logging.getLogger(__name__)

class TaskScheduler:
    _instance = None
    _started = False
    _last_mtime = 0
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TaskScheduler, cls).__new__(cls)
            cls._instance.tasks = {}
            cls._instance.task_file = os.path.join(Config.get_config_dir(), "tasks.json")
            cls._instance.pid_file = os.path.join(Config.get_config_dir(), "scheduler.pid")
            cls._instance.verbose = False
            cls._instance.load_tasks()
        return cls._instance

    def load_tasks(self):
        if not os.path.exists(self.task_file):
            self.tasks = {}
            return

        try:
            mtime = os.path.getmtime(self.task_file)
            if mtime > self._last_mtime:
                with open(self.task_file, 'r', encoding='utf-8') as f:
                    self.tasks = json.load(f)
                self._last_mtime = mtime
                # logger.debug(f"Tasks reloaded from file (mtime: {mtime})")
        except Exception as e:
            logger.error(f"Failed to load tasks: {e}")

    def save_tasks(self):
        try:
            with open(self.task_file, 'w', encoding='utf-8') as f:
                json.dump(self.tasks, f, indent=4, ensure_ascii=False)
            # Update mtime after write to avoid reloading own changes
            self._last_mtime = os.path.getmtime(self.task_file)
        except Exception as e:
            logger.error(f"Failed to save tasks: {e}")

    def add_price_alert(self, ts_code, operator, threshold, email=None):
        self.load_tasks()
        task_id = f"price_alert_{ts_code}_{int(time.time())}"
        task = {
            "id": task_id,
            "type": "price_alert",
            "ts_code": ts_code,
            "operator": operator,
            "threshold": float(threshold),
            "email": email or Config.EMAIL_RECEIVER or Config.EMAIL_SENDER,
            "enabled": True,
            "created_at": time.time()
        }
        self.tasks[task_id] = task
        self.save_tasks()
        return task_id

    def update_price_alert(self, task_id, ts_code=None, operator=None, threshold=None):
        self.load_tasks()
        if task_id not in self.tasks:
            return False
            
        task = self.tasks[task_id]
        if ts_code:
            task['ts_code'] = ts_code
        if operator:
            task['operator'] = operator
        if threshold is not None:
            task['threshold'] = float(threshold)
            
        # If updating, re-enable it if it was disabled/fired
        task['enabled'] = True
        
        self.save_tasks()
        return True

    def list_tasks(self):
        self.load_tasks()
        return list(self.tasks.values())

    def remove_task(self, task_id):
        self.load_tasks()
        if task_id in self.tasks:
            del self.tasks[task_id]
            self.save_tasks()
            return True
        return False

    def check_conditions(self):
        # In interactive mode (not verbose), yield to worker if one is running
        if not self.verbose and self._is_worker_running():
            return

        self.load_tasks()
        
        if self.verbose:
            enabled_count = sum(1 for t in self.tasks.values() if t.get('enabled', True))
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Checking {len(self.tasks)} tasks ({enabled_count} enabled)...")

        if not self.tasks:
            return

        for task_id, task in self.tasks.items():
            if not task.get('enabled', True):
                if self.verbose:
                    print(f"  [Task {task_id}] Skipped (Disabled)")
                continue
                
            if task['type'] == 'price_alert':
                self._check_price_alert(task)

    def _check_price_alert(self, task):
        from fin_agent.tools.tushare_tools import get_realtime_price
        
        try:
            ts_code = task['ts_code']
            operator = task['operator']
            threshold = task['threshold']
            email = task['email']
            
            # Fetch Price
            # Note: Tushare limits. We should probably cache this if many tasks watch same stock.
            # But for simple version, fetch one by one.
            price_info = get_realtime_price(ts_code)
            
            if not price_info:
                logger.warning(f"Could not get price for {ts_code}")
                return
                
            # Parse price (get_realtime_price returns a formatted string or list, 
            # let's assume it returns a dict-like or we parse the tool output)
            # Actually tool returns a string. We need internal function to get raw data for robustness.
            # For now, let's use tushare API directly via tools helper if available, 
            # OR parse the string. 
            # Better: use get_realtime_price but we need the float value.
            
            # Re-implementing simple fetch here to avoid parsing the AI-friendly string output of tools
            import tushare as ts
            pro = ts.pro_api(Config.TUSHARE_TOKEN)
            
            # Use Tushare's standard realtime API if available, otherwise fallback to legacy
            # NOTE: tushare.pro.realtime_quote is NOT a standard PRO interface.
            # Tushare PRO uses 'get_realtime_quotes' from legacy 'ts' package for free realtime data (Sina source).
            # The 'pro' object typically handles historical data.
            #
            # The error "请指定正确的接口名" usually means we called a non-existent method on pro_api.
            # pro.realtime_quote() is likely invalid.
            #
            # Let's use the legacy method which works for realtime snapshot.
            
            # df = pro.realtime_quote(ts_code=ts_code) # This was causing the error
            
            # Using legacy ts.get_realtime_quotes
            # It expects code like '000001', '600519', no suffix usually, but let's try handling suffix.
            code_parts = ts_code.split('.')
            code_no_suffix = code_parts[0]
            
            df = ts.get_realtime_quotes(code_no_suffix)
            
            if df is None or df.empty:
                 logger.warning(f"No data for {ts_code}")
                 return

            # Legacy df columns are: name, open, pre_close, price, high, low, bid, ask, volume, amount, date, time, code
            current_price = float(df.iloc[0]['price'])
            
            if self.verbose:
                print(f"  [Task {task['id']}] {ts_code}: {current_price} (Target: {operator} {threshold})")

            # Special case for "0" price (suspension or error)
            if current_price == 0:
                return
            
            record = df.iloc[0].to_dict()
            
            # Compare
            triggered = False
            if operator == ">" and current_price > threshold:
                triggered = True
            elif operator == ">=" and current_price >= threshold:
                triggered = True
            elif operator == "<" and current_price < threshold:
                triggered = True
            elif operator == "<=" and current_price <= threshold:
                triggered = True
                
            if triggered:
                stock_name = record.get('name', ts_code)
                # 优化邮件标题，使其看起来更正规，减少被识别为垃圾邮件的可能
                subject = f"[Fin-Agent] 股价提醒: {stock_name} ({ts_code}) 触发条件"
                
                # 优化纯文本内容
                content = (
                    f"股价提醒通知\n"
                    f"================================\n"
                    f"股票名称: {stock_name}\n"
                    f"股票代码: {ts_code}\n"
                    f"当前价格: {current_price}\n"
                    f"触发条件: 价格 {operator} {threshold}\n"
                    f"触发时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"================================\n"
                    f"此邮件由 Fin-Agent 自动发送。"
                )
                
                # 添加HTML内容，提升邮件质量
                price_color = "#d9534f" if operator.startswith('>') else "#5cb85c" # 涨红跌绿(或根据涨跌逻辑调整)
                html_content = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="utf-8">
                    <title>股价提醒</title>
                    <style>
                        body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; background-color: #f4f4f4; }}
                        .container {{ max-width: 600px; margin: 20px auto; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); overflow: hidden; }}
                        .header {{ background-color: #0056b3; color: #ffffff; padding: 20px; text-align: center; }}
                        .header h2 {{ margin: 0; font-size: 24px; }}
                        .content {{ padding: 30px; }}
                        .stock-info {{ background-color: #f9f9f9; padding: 15px; border-radius: 5px; margin-bottom: 20px; border-left: 4px solid #0056b3; }}
                        .info-row {{ display: flex; justify-content: space-between; margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 5px; }}
                        .info-row:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
                        .label {{ font-weight: bold; color: #666; }}
                        .value {{ font-weight: 500; color: #333; }}
                        .price {{ font-size: 18px; font-weight: bold; color: {price_color}; }}
                        .footer {{ background-color: #eee; color: #888; padding: 15px; text-align: center; font-size: 12px; }}
                    </style>
                </head>
                <body>
                    <div class="container">
                        <div class="header">
                            <h2>股价提醒通知</h2>
                        </div>
                        <div class="content">
                            <p>您好，您关注的股票已触发提醒条件：</p>
                            <div class="stock-info">
                                <div class="info-row">
                                    <span class="label">股票名称</span>
                                    <span class="value">{stock_name}</span>
                                </div>
                                <div class="info-row">
                                    <span class="label">股票代码</span>
                                    <span class="value">{ts_code}</span>
                                </div>
                                <div class="info-row">
                                    <span class="label">当前价格</span>
                                    <span class="value price">{current_price}</span>
                                </div>
                                <div class="info-row">
                                    <span class="label">触发条件</span>
                                    <span class="value">价格 {operator} {threshold}</span>
                                </div>
                            </div>
                            <p>触发时间：{time.strftime('%Y-%m-%d %H:%M:%S')}</p>
                        </div>
                        <div class="footer">
                            <p>此邮件由 Fin-Agent 智能助手自动发送，请勿直接回复。</p>
                        </div>
                    </div>
                </body>
                </html>
                """
                
                print(f"\n[Scheduler] Triggering task {task['id']}: {subject}")
                success = NotificationManager.send_email(subject, content, email, html_content=html_content)
                
                if success:
                    print(f"[Scheduler] Email sent to {email}")
                else:
                    print(f"[Scheduler] Failed to send email to {email}")

                # Disable task after firing (one-time alert)
                task["enabled"] = False
                self.save_tasks()
                    
        except Exception as e:
            if "403" in str(e) and "Forbidden" in str(e):
                 msg = f"Tushare API 403 Forbidden. Please check your token validity and permissions."
                 if self.verbose:
                     print(f"  [Task {task['id']}] Error: {msg}")
                 else:
                     # In interactive mode, print to stderr or just log
                     logger.error(f"Error checking task {task['id']}: {msg}")
            else:
                logger.error(f"Error checking task {task['id']}: {e}")

    def run_scheduler(self, cycle=10):
        # Schedule the check every cycle minutes
        # For stricter timing, we could do every 10 seconds, but Tushare has rate limits.
        # Default is 10 minutes.
        schedule.every(cycle).minutes.do(self.check_conditions)
        
        last_heartbeat = 0
        
        while True:
            try:
                # In worker mode, ensure PID file exists (restore if deleted by others)
                # AND update modification time as a heartbeat
                if self.verbose:
                    current_time = time.time()
                    if current_time - last_heartbeat > 5: # Every 5 seconds
                        try:
                            if not os.path.exists(self.pid_file):
                                with open(self.pid_file, 'w') as f:
                                    f.write(str(os.getpid()))
                                logger.warning("Restored missing PID file.")
                            else:
                                # Update mtime to indicate liveness
                                os.utime(self.pid_file, None)
                            
                            last_heartbeat = current_time
                        except Exception as e:
                            logger.error(f"Failed to update PID file heartbeat: {e}")

                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
                time.sleep(5)

    def _is_worker_running(self):
        """Check if a worker process is running using PID file heartbeat."""
        if not os.path.exists(self.pid_file):
            return False
            
        try:
            # Check if file was updated recently (heartbeat)
            # This avoids using os.kill which can be problematic on Windows
            mtime = os.path.getmtime(self.pid_file)
            age = time.time() - mtime
            
            # If heartbeat is within 20 seconds (worker updates every 5s), it's alive.
            if age < 20:
                return True
                
            # If file is older, it might be stale.
            # We assume it's NOT running to avoid blocking the interactive scheduler forever
            # in case of a crash.
            
            # Note: This means if the user is running an OLD version of the worker
            # that doesn't update mtime, this will return False, and we will start
            # a duplicate scheduler. This is a safe degradation compared to killing the process.
            
            # If it's stale (older than 20s), we should check if the process actually exists
            try:
                with open(self.pid_file, 'r') as f:
                    pid = int(f.read().strip())
                
                # Check process existence
                if platform.system() == "Windows":
                    # Windows
                    # OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
                    # or just try tasklist / psutil. 
                    # Simpler: os.kill(pid, 0) works on Windows to check existence (permissions aside)
                    # But Python's os.kill on Windows does TerminateProcess if signal is not 0?
                    # No, os.kill(pid, 0) is supported on Windows since Python 2.7 to check validity.
                    os.kill(pid, 0)
                else:
                    # Unix
                    os.kill(pid, 0)
                    
                # If we get here, process exists but hasn't updated heartbeat.
                # Maybe it's stuck. We still treat it as "running" to be safe?
                # Or if it is VERY old, we assume dead? 
                # Let's trust the heartbeat. If heartbeat failed, worker might be frozen.
                # But here we just want to know if we should clean up the PID file.
                return False # It exists but is frozen/stale heartbeat. 
                             # Actually, if it exists, we shouldn't delete the PID file blindly.
                             
            except OSError:
                # Process does not exist
                logger.warning(f"Found stale PID file (PID {pid} not running). Removing.")
                try:
                    # Force remove just in case
                    if os.path.exists(self.pid_file):
                         os.remove(self.pid_file)
                         logger.warning(f"Removed stale PID file: {self.pid_file}")
                except Exception as e:
                    logger.error(f"Failed to remove stale PID file: {e}")
                return False
            except Exception:
                # Reading PID failed, maybe file is empty or corrupted
                try:
                    if os.path.exists(self.pid_file):
                         os.remove(self.pid_file)
                except:
                    pass
                return False
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking worker status: {e}")
            return False

    def start(self):
        """
        Start the scheduler in a background thread.
        Will NOT start if a worker process is detected via PID file.
        """
        if self._started:
            return

        if self._is_worker_running():
            # In interactive mode, we don't want to print to stdout as it might interfere with TUI
            # But fin-agent is CLI based, so print is fine.
            # print(f"[{time.strftime('%H:%M:%S')}] Detected active Worker process. Interactive scheduler disabled to avoid duplicates.")
            return

        t = threading.Thread(target=self.run_scheduler, args=(10,), daemon=True)
        t.start()
        self._started = True
        # print("Background scheduler started.") 

    def run_forever(self, cycle=10):
        """Run the scheduler in blocking mode (Worker Mode)."""
        print(f"Starting scheduler worker (interval: {cycle}m)... (Press Ctrl+C to stop)")
        print(f"Task file: {self.task_file}")
        
        # Write PID file
        pid = os.getpid()
        with open(self.pid_file, 'w') as f:
            f.write(str(pid))
            
        try:
            self.verbose = True
            self.run_scheduler(cycle=cycle)
        except KeyboardInterrupt:
            print("\nWorker stopped by user.")
        except Exception as e:
            logger.error(f"Worker crashed: {e}")
            print(f"\nWorker crashed: {e}")
        finally:
            # Clean up PID file on exit
            if os.path.exists(self.pid_file):
                try:
                    # Check if it's still our PID before deleting
                    with open(self.pid_file, 'r') as f:
                        content = f.read().strip()
                    if content == str(pid):
                        os.remove(self.pid_file)
                except Exception:
                    pass
