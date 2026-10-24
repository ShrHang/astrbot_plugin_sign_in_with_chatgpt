# 项目结构

本项目为 AstrBot 的 Sign in with ChatGPT 供应商插件。

| 路径 | 说明 |
| --- | --- |
| `main.py` | 插件入口与初始化、停止生命周期 |
| `siwc/auth.py` | OAuth 授权、回调和身份校验 |
| `siwc/credentials.py` | 凭据持久化、权限保护和进程锁 |
| `siwc/service.py` | 账号状态、令牌刷新、模型目录和共享服务 |
| `siwc/provider.py` | AstrBot Provider 适配与推理请求生命周期 |
| `siwc/responses.py` | 请求转换、SSE 解析、响应和引用处理 |
| `siwc/registration.py` | Provider 注册、实例恢复与卸载 |
| `siwc/management.py` | 账号管理接口与后台授权任务 |
| `siwc/errors.py` | 错误分类与安全诊断信息 |
| `siwc/__init__.py` | 插件标识与 SIWC 协议常量 |
| `views/account/` | 账号管理页面的 HTML、CSS 和 JavaScript |
| `manage.py` | CLI 诊断与离线账号管理 |
| `metadata.yaml` | 插件元数据与宿主版本兼容声明 |
| `_conf_schema.json` | 插件配置项定义 |
| `requirements.txt` | Python 依赖声明 |
| `README.md` | 插件介绍、安装使用与当前限制 |
