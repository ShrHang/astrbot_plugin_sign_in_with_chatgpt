"""Run while the plugin is disabled: python manage.py --data-dir ... login/status/logout/models/check."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path

import httpx

from siwc import PLUGIN_NAME, RESOURCE
from siwc.errors import SIWCError, http_error
from siwc.responses import completed_response, sse_events
from siwc.service import Service


async def resolve_proxy(directory: Path, explicit: str | None):
    """CLI override > this data root's plugin setting > direct connection."""
    if explicit is not None:
        return explicit, "命令行"
    directory = directory.resolve()
    if directory.name != PLUGIN_NAME or directory.parent.name != "plugin_data":
        return "", "默认直连（自定义数据目录，请用 --proxy 指定代理）"
    path = directory.parent.parent / "config" / f"{PLUGIN_NAME}_config.json"
    try:
        contents = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
    except FileNotFoundError:
        return "", "默认直连（未找到插件配置）"
    except OSError:
        raise SIWCError("config", "无法读取插件代理设置；请检查配置权限或显式传入 --proxy") from None
    try:
        config = json.loads(contents)
        proxy = config.get("proxy", "")
        if not isinstance(proxy, str):
            raise ValueError
    except (ValueError, AttributeError):
        raise SIWCError("config", "插件代理设置格式无效；请修正配置或显式传入 --proxy") from None
    return proxy, "插件设置"


async def main(args):
    root = Path(os.environ.get("ASTRBOT_ROOT", Path.home() / ".astrbot"))
    directory = args.data_dir or root / "data" / "plugin_data" / PLUGIN_NAME
    proxy, source = await resolve_proxy(directory, args.proxy)
    service = Service(directory, {"proxy": proxy})
    try:
        if args.command in {"diagnose", "login"}:
            print(f"网络配置来源：{source}。")
        if args.command == "diagnose":
            async with asyncio.timeout(service.total_timeout):
                await service.oauth.prepare_identity()
            print("公开 OIDC 发现文档与 JWKS 可访问；未打开浏览器、读取凭据或验证账号权限。")
            return
        await service.start()
        if args.command == "login":
            print("正在检查公开身份校验端点；通过后打开系统浏览器。Ctrl+C 取消。")

            async def browser(url):
                print("Continue with ChatGPT：请在浏览器完成授权，最终结果以本终端为准。")
                if not await asyncio.to_thread(webbrowser.open, url):
                    raise SIWCError("browser", "系统浏览器未能打开；请检查默认浏览器设置后重试")

            await service.login(browser, port=args.port, timeout=args.login_timeout)
            print("已验证身份与订阅推理权限，凭据已保存。实际模型调用请运行 check。")
        elif args.command == "status":
            print(json.dumps(service.status(), ensure_ascii=False, indent=2))
        elif args.command == "logout":
            confirmed = await service.logout()
            print("已清除本地凭据并保留注册信息。" + ("远端撤销已确认。" if confirmed else "远端撤销未确认，请在 ChatGPT 设置中断开应用。"))
        elif args.command == "models":
            print("\n".join(await service.models()))
        elif args.command == "check":
            if not args.model:
                raise SIWCError("request", "check 需要 --model 指定 models 返回的 slug")
            async with asyncio.timeout(service.total_timeout):
                token = await service.access_token()
                async with service.client.stream("POST", RESOURCE + "/responses",
                    headers={"Authorization": "Bearer " + token, "Accept": "text/event-stream"},
                    json={"model": args.model, "input": [{"role": "user", "content": "Reply exactly PONG."}],
                          "store": False, "stream": True}) as response:
                    if response.is_error:
                        await response.aread()
                        raise http_error(response, stage="inference")
                    async for event in sse_events(response):
                        if completed_response(event) is not None:
                            print("已收到 response.completed，最小推理成功。")
                            return
                    raise SIWCError("incomplete", "连接断开，未收到成功终止事件")
    finally:
        await service.close()


def cli():
    parser = argparse.ArgumentParser(description="AstrBot SIWC 本地账号管理（请先停用插件）")
    parser.add_argument("--data-dir", type=Path, help="StarTools.get_data_dir() 对应的绝对目录")
    parser.add_argument("--proxy", default=None, help="覆盖插件代理设置；空字符串强制直连，不读取环境代理")
    parser.add_argument("--port", type=int, default=0, help="回环回调端口，0 为系统分配")
    parser.add_argument("--login-timeout", type=float, default=180)
    parser.add_argument("--model", default="")
    parser.add_argument("command", choices=["login", "status", "logout", "models", "check", "diagnose"])
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("操作已取消。", file=sys.stderr)
        return 130
    except SIWCError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (httpx.HTTPError, TimeoutError):
        print("网络失败或超时，请检查显式代理与网络连接。", file=sys.stderr)
        return 1
    except Exception as error:
        # Do not print exception text: third-party errors may contain callback URLs.
        print(f"操作失败（{type(error).__name__}），请检查本地存储权限与运行环境。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli())
