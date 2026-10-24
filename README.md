# Sign in with ChatGPT

> [!WARNING]
> 本项目为个人插件，目前仅在 AstrBot Desktop Windows 上测试过，不保证在其他环境下稳定运行。

> [!IMPORTANT]
> 需要 AstrBot 4.29.1+

Sign in with ChatGPT 是面向 Windows / AstrBot 4.29.1 的插件，通过官方账号授权接入 ChatGPT 订阅模型。

支持文字、图片输入、函数工具往返、流式响应和可选的独立网页搜索工具。暂不支持音频、视频、文件上传及生图。

## 安装与授权

1. 在 AstrBot 插件页通过本插件的 GitHub 仓库地址安装，并保持插件启用。已有绑定可直接复用。
2. 打开本插件的 **account** 页面，点击 **Continue with ChatGPT**，在 AstrBot 所在电脑的系统浏览器完成授权。
3. 等待管理页显示“已绑定”；登录阶段和倒计时显示在连接状态区域。授权过程中，同一按钮显示“取消授权”；已有绑定时显示“重新授权”。收到浏览器回调时仍可能正在验证，关闭或刷新页面不会取消后台授权。
4. 在 AstrBot 模型页面添加“Sign in with ChatGPT”提供商源，新建源的默认 ID 为 `SIWC`。选择账号可用模型，Key 保持 `siwc-managed-no-api-key` 占位值。管理页可以刷新模型列表；模型测试使用 AstrBot 供应商配置页的功能。

管理页也提供重新授权和解绑。重新授权期间暂停新的本账号模型请求；失败或取消会保留原有效绑定。

## 插件设置

配置按以下四组显示，标题下方的说明提供作用范围、单位和使用限制。保存后由 AstrBot 热重载生效。

| 分组 | 配置项 | 用途 |
| --- | --- | --- |
| 供应商接入 | 启用供应商 | 控制供应商类型、模型实例和搜索工具 |
| 网页搜索 | 启用搜索工具、搜索模型、搜索超时、摘要长度上限 | 配置独立检索模型与返回内容 |
| 网络连接 | 代理地址、连接超时、读取超时 | 统一控制授权、令牌刷新、模型目录和推理的网络连接 |
| 请求与并发 | 请求总超时、最大并发数 | 限制调用耗时和共享并发数量 |

“供应商接入 → 启用供应商”关闭后，撤下类型选项并停止模型实例和搜索工具，保留模型配置和账号绑定；重新开启后恢复已启用的模型实例。账号管理页面仍可使用。

## 独立网页搜索工具(beta)

1. 在 AstrBot 模型页面配置并启用一个 Sign in with ChatGPT 模型，确认账号已绑定。
2. 在插件设置的“网页搜索”分组中选择“搜索模型”，开启“启用搜索工具”；“供应商接入 → 启用供应商”也需保持开启。
3. 确保当前会话的主模型支持工具调用，并在 AstrBot 的工具/人设设置中允许使用 `siwc_web_search`。主模型可以使用 Sign in with ChatGPT 或其他供应商。

调用流程为：主模型提交自包含的 `query` → 指定搜索模型执行 OpenAI 原生搜索 → 工具返回摘要与来源 → 主模型结合对话组织最终回答。搜索请求不携带聊天历史和 AstrBot 函数工具，不会递归调用 `siwc_web_search`；不支持搜索的模型、未完成的搜索及超时会返回明确错误，不会冒充搜索成功。

工具结果包含 `ok`、`searched`、`search_count`、`summary`、`sources` 和 `truncated`；失败时返回 `ok: false` 与安全错误信息。来源中 `cited: true` 表示搜索摘要引用的来源，`false` 表示搜索过程返回的其他参考来源；来源按 URL 去重并最多保留 32 条。摘要可设置长度上限，默认 12000 字，超出后标记截断；摘要和来源中的链接会保留，主模型负责最终表述，不能保证回答完全无链接。使用 OpenAI 搜索结果时仍需遵循其[来源引用展示要求](https://developers.openai.com/api/docs/guides/tools-web-search#output-and-citations)。

独立搜索默认超时为 90 秒，实际不超过插件的单次调用总预算；应小于 AstrBot 的工具调用超时。搜索会额外消耗一次或多次模型推理及检索资源，可用性受所选模型和账号策略限制。建议关闭其他重复的搜索工具，插件不会代为修改宿主设置。

普通模型对话支持文本流式输出及 AstrBot 函数工具，OpenAI 原生搜索仅通过独立搜索工具调用。

## 打包发布

在项目根目录运行 `python dist/build.py`，仅需 Python 3.10+ 标准库，无需安装 AstrBot 或插件依赖。脚本根据 `metadata.yaml` 的名称和版本号，在 `dist/` 生成 `<name>-<version>.zip` 和对应的 `.zip.sha256` 校验文件；同版本再次运行会覆盖这两个产物。也可用 `--output-dir <目录>` 指定输出目录，脚本支持从任意工作目录调用。

ZIP 内保留一层插件名称目录，打包清单如下：

| 文件 | 用途 |
| --- | --- |
| `main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt` | 插件入口、宿主识别、配置和依赖安装 |
| `siwc/` 下的 11 个 Python 模块 | 配置读取、授权、凭据、服务、Provider、响应处理、注册、独立搜索工具及管理接口 |
| `views/account/index.html`、`style.css`、`app.js` | 完整账号管理页面；桥接 SDK 由 AstrBot 提供 |
| `manage.py` | 保留 CLI 诊断与离线账号管理能力 |
| `README.md`、`LICENSE` | 使用说明和完整开源许可证 |

打包采用 `dist/build.py` 中的明确文件清单，不包含 `.git/`、`.venv/`、缓存、日志、账号凭据、`AGENTS.md`、测试、打包脚本和历史产物。新增运行模块或页面资源时需同步更新清单；清单文件缺失会中止打包。

## 开源许可

Copyright (C) 2026 ShrHang.

本项目采用 GNU Affero General Public License v3.0（`AGPL-3.0-only`），完整条款见 [LICENSE](LICENSE)。

本插件继承并复用 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 Provider 和上下文处理能力。AstrBot 本体由其原作者及贡献者持有版权，采用 [AGPL-3.0](https://github.com/AstrBotDevs/AstrBot/blob/master/LICENSE)；本仓库不包含 AstrBot 本体。第三方依赖遵循各自的许可证。
