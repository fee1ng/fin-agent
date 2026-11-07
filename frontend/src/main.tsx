import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import { AuthProvider } from './auth';
import './style.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#256354', borderRadius: 8, fontFamily: 'Inter, "Microsoft YaHei", sans-serif' } }}>
    <AntApp><BrowserRouter><AuthProvider><App /></AuthProvider></BrowserRouter></AntApp>
  </ConfigProvider></React.StrictMode>,
);
