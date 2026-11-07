"""Local administrator commands. Passwords never appear in process arguments."""
import argparse
import getpass
import json
from sqlalchemy.exc import IntegrityError

from fin_agent.web.auth import create_user, set_disabled
from fin_agent.web.database import Database
from fin_agent.web.settings import Settings


def main():
    parser = argparse.ArgumentParser(description='Fin-Agent Web administrator')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('migrate')
    sub.add_parser('worker')
    sub.add_parser('health').add_argument('service', choices=('api', 'worker'))
    cleanup = sub.add_parser('cleanup')
    cleanup.add_argument('--backup-dir')
    sub.add_parser('backup').add_argument('destination')
    sub.add_parser('restore').add_argument('source')
    create = sub.add_parser('create-user')
    create.add_argument('username')
    create.add_argument('--admin', action='store_true')
    for action in ('disable-user', 'enable-user'):
        sub.add_parser(action).add_argument('username')
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == 'health':
        if args.service == 'api':
            from urllib.request import urlopen
            with urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3) as response:
                if response.status != 200:
                    raise SystemExit(1)
        else:
            from fin_agent.web.runs import Runs
            database = Database(settings.database)
            try:
                if not Runs(database, settings).executor_status()['available']:
                    raise SystemExit(1)
            finally:
                database.close()
        return
    if args.command == 'worker':
        from fin_agent.web.worker import Worker
        try:
            Worker(settings).serve()
        except KeyboardInterrupt:
            pass
        return
    if args.command == 'serve':
        import uvicorn
        from fin_agent.web.api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    database = Database(settings.database)
    try:
        if args.command == 'migrate':
            database.migrate()
            print('Web database migrated')
        elif args.command == 'backup':
            from fin_agent.web.backups import backup_file
            database.check_schema()
            print(json.dumps(backup_file(database, args.destination)))
        elif args.command == 'restore':
            from fin_agent.web.backups import restore_file
            print(json.dumps(restore_file(database, args.source)))
        elif args.command == 'cleanup':
            from fin_agent.web.maintenance import cleanup as purge
            from fin_agent.web.backups import prune_backups
            database.check_schema()
            result = purge(database, settings)
            result['backups_pruned'] = prune_backups(database, args.backup_dir or database.path.parent / 'backups', settings.backup_days)
            print(json.dumps(result))
        elif args.command == 'create-user':
            database.check_schema()
            password = getpass.getpass('Password (12+ characters): ')
            if password != getpass.getpass('Confirm password: '):
                parser.error('Passwords do not match')
            create_user(database, args.username, password, 'admin' if args.admin else 'user')
            print('Account created')
        else:
            database.check_schema()
            set_disabled(database, args.username, args.command == 'disable-user')
            print('Account updated; existing logins revoked')
    except IntegrityError:
        parser.error('Account already exists or violates a database constraint')
    except ValueError as exc:
        parser.error(str(exc))
    finally:
        database.close()


if __name__ == '__main__':
    main()
