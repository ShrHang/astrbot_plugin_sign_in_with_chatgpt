# ChatGPT 订阅

> [!WARNING]
> 本项目为个人插件，目前仅在 AstrBot Desktop Windows 上测试过，不保证在其他环境下稳定运行。

> [!IMPORTANT]
> 需要 AstrBot 4.29.1+

面向 Windows / AstrBot 4.29.1 的个人插件，通过正式 Sign in with ChatGPT 接入 ChatGPT 订阅模型。

支持文字、图片输入、函数工具往返、流式响应和可选的 OpenAI 原生网页搜索。暂不支持音频、视频、文件上传及生图。

## 安装与授权

1. 在 AstrBot 插件页通过本插件的 GitHub 仓库地址安装，并保持插件启用。已有绑定可直接复用。
2. 打开本插件的 **account** 页面，点击 **Continue with ChatGPT**，在 AstrBot 所在电脑的系统浏览器完成授权。
3. 等待管理页显示“已绑定”；登录阶段和倒计时显示在连接状态区域。授权过程中，同一按钮显示“取消授权”；已有绑定时显示“重新授权”。收到浏览器回调时仍可能正在验证，关闭或刷新页面不会取消后台授权。
4. 在 AstrBot 模型页面添加“ChatGPT 订阅”，选择账号可用模型，Key 保持 siwc-managed-no-api-key 占位值。管理页可以刷新模型列表；模型测试使用 AstrBot 供应商配置页的功能。

管理页也提供重新授权和解绑。重新授权期间暂停新的本账号模型请求；失败或取消会保留原有效绑定。

插件设置中的“启用 ChatGPT 订阅供应商接入”控制供应商类型及模型实例。关闭后撤下类型选项并停止模型实例，保留已有供应商/模型配置和账号绑定；重新启用后恢复已启用的模型实例。账号管理页仍可使用。

## 原生网页搜索(beta)

插件设置中的“是否开启 OpenAI 原生网页搜索”默认关闭。开启后，每次 SIWC Provider 请求允许模型按需使用 OpenAI 托管搜索，是否搜索由模型决定，可用性受模型及账号策略限制；原有函数工具仍可使用。

建议同时关闭 AstrBot 自带网络搜索，避免重复检索。插件不会代为修改宿主或其他插件设置；两者同时开启时不保证只使用其中一种搜索。

目前原生网页搜索结果会返回网页链接，这一点无法靠提示词约束，有待完善。

## 开源许可

Copyright (C) 2026 ShrHang.

本项目采用 GNU Affero General Public License v3.0（`AGPL-3.0-only`），完整条款见 [LICENSE](LICENSE)。

本插件继承并复用 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 Provider 和上下文处理能力。AstrBot 本体由其原作者及贡献者持有版权，采用 [AGPL-3.0](https://github.com/AstrBotDevs/AstrBot/blob/master/LICENSE)；本仓库不包含 AstrBot 本体。第三方依赖遵循各自的许可证。
