"""B 站 Cookie 辅助登录交互管理器。"""

import asyncio
import os
import tempfile
import time
from typing import Optional, Any

import aiohttp
import qrcode

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Plain

from ....logger import logger
from ...base import AdminAssistManager


class BilibiliAdminCookieAssistManager(AdminAssistManager):
    """B站Cookie管理员协助登录状态机（插件侧后台触发，不阻塞解析链）。

    管理员通过官方指令（默认 ``bilibili登录``）主动发起扫码登录；
    Cookie 失效时插件也会私聊通知管理员发送该指令。指令由 AstrBot
    原生命令系统接管，不会再被转交给 LLM。
    """

    QR_CODE_TTL_SECONDS = 180

    def __init__(
        self,
        context,
        admin_id: str,
        enabled: bool,
        reply_timeout_minutes: int,
        request_cooldown_minutes: int,
    ):
        """初始化 B 站 Cookie 辅助管理器。"""
        super().__init__(
            context=context,
            admin_id=admin_id,
            enabled=enabled,
            reply_timeout_minutes=reply_timeout_minutes,
            request_cooldown_minutes=request_cooldown_minutes,
        )
        self._login_in_progress = False

    async def start_login_flow(
        self, event: AstrMessageEvent, auth_runtime: Optional[Any]
    ) -> bool:
        """生成二维码并启动一次扫码轮询。

        Returns:
            bool: 是否成功发起扫码登录。
        """
        if auth_runtime is None:
            await event.send(
                event.plain_result("B站登录运行时未初始化，无法发起协助登录。")
            )
            return False

        async with self._lock:
            already_running = self._login_in_progress
            if not already_running:
                self._login_in_progress = True
        if already_running:
            await event.send(
                event.plain_result("已有一轮B站扫码登录正在进行，请先完成或等待其结束。")
            )
            return False

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = await auth_runtime.generate_login_payload(session)
            await self._send_local_login_qr(event, payload["login_url"])
            self._new_task(
                self._poll_login_and_notify(
                    event=event,
                    auth_runtime=auth_runtime,
                    qrcode_key=payload["qrcode_key"],
                    unified_msg_origin=event.unified_msg_origin,
                )
            )
            return True
        except asyncio.CancelledError:
            self._login_in_progress = False
            raise
        except Exception as exc:
            self._login_in_progress = False
            logger.warning(f"[bilibili] 生成管理员协助登录链接失败: {exc}")
            await event.send(event.plain_result("生成B站登录链接失败，请稍后重试。"))
            return False

    def trigger_assist_request(self, reason: str) -> None:
        """发起一次管理员辅助登录请求（仅通知，等待管理员发送官方指令）。"""
        if not self.enabled:
            return
        self._new_task(self._trigger_assist_request(reason))

    async def _trigger_assist_request(self, reason: str) -> None:
        """异步执行辅助登录通知提交流程。"""
        async with self._lock:
            now = time.monotonic()
            if self._login_in_progress:
                return
            if now - self._last_request_at < self.request_cooldown_seconds:
                return
            if not self._admin_private_origin:
                logger.warning(
                    "[bilibili] 无管理员私聊会话可用，无法主动发送Cookie协助请求。"
                )
                return

            previous_request_at = self._last_request_at
            self._last_request_at = now
            unified_msg_origin = self._admin_private_origin

        reason_text = reason or "cookie_unavailable"
        try:
            await self._send_private_text(
                unified_msg_origin,
                "检测到B站Cookie不可用，是否协助登录？\n"
                "请直接发送指令 bilibili登录 发起扫码登录，其他任何消息均无需处理。\n"
                f"本次原因: {reason_text}",
            )
        except Exception:
            async with self._lock:
                self._last_request_at = previous_request_at
            raise

    @staticmethod
    def _create_local_qr_code(login_url: str) -> str:
        """Render a login QR code locally without disclosing its token."""
        fd, qr_path = tempfile.mkstemp(prefix="astrbot_bilibili_qr_", suffix=".png")
        os.close(fd)
        try:
            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=4,
            )
            qr.add_data(login_url)
            qr.make(fit=True)
            qr.make_image(fill_color="black", back_color="white").save(qr_path)
            return qr_path
        except Exception:
            try:
                os.remove(qr_path)
            except OSError:
                pass
            raise

    async def _send_local_login_qr(
        self, event: AstrMessageEvent, login_url: str
    ) -> None:
        """Send a locally rendered QR image plus an independent login link.

        The login link is sent as its own message so it is never dropped when a
        platform renders a mixed image+text chain by discarding the text part.
        """
        qr_path = await asyncio.to_thread(self._create_local_qr_code, login_url)
        try:
            chain = [
                Plain("请使用哔哩哔哩客户端扫描下方二维码完成登录："),
                Image.fromFileSystem(qr_path),
            ]
            await event.send(event.chain_result(chain))
        except Exception as image_error:
            logger.warning(
                "[bilibili] 发送本地登录二维码失败，回退为登录链接: "
                f"{type(image_error).__name__}"
            )
        finally:
            try:
                os.remove(qr_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(f"[bilibili] 清理临时登录二维码失败: {exc}")

        # 独立的链接登录消息，保证一定可见（二维码与链接二选一即可）。
        try:
            await event.send(
                event.plain_result(
                    "或直接点击链接登录（在浏览器/客户端打开后授权即可）：\n"
                    f"{login_url}"
                )
            )
        except Exception as link_error:
            logger.warning(f"[bilibili] 发送登录链接失败: {type(link_error).__name__}")

    async def _poll_login_and_notify(
        self,
        event: AstrMessageEvent,
        auth_runtime: Any,
        qrcode_key: str,
        unified_msg_origin: str,
    ) -> None:
        """异步轮询登录状态并向管理员反馈结果。

        通过 ``event.send`` 上报「已扫码待确认」进度（与二维码同通道，保证可见），
        登录成功/失败仍走原提示逻辑。
        """
        scanned_notified = False

        async def _on_progress(state: str) -> None:
            nonlocal scanned_notified
            if state == "scanned" and not scanned_notified:
                scanned_notified = True
                try:
                    await event.send(
                        event.plain_result("已检测到扫码，请在手机/B站客户端确认登录。")
                    )
                except Exception as exc:
                    logger.warning(f"[bilibili] 发送已扫码提示失败: {type(exc).__name__}")

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    result = await auth_runtime.poll_login_until_complete(
                        session=session,
                        qrcode_key=qrcode_key,
                        timeout_seconds=min(
                            self.reply_timeout_seconds, self.QR_CODE_TTL_SECONDS
                        ),
                        on_progress=_on_progress,
                    )
            except Exception as exc:
                logger.warning(
                    f"[bilibili] 管理员协助登录轮询失败: {type(exc).__name__}"
                )
                await self._send_private_text(
                    unified_msg_origin, "B站登录轮询失败，请稍后重试。"
                )
                return

            status = result.get("status")
            if status == "success":
                await self._send_private_text(
                    unified_msg_origin,
                    "B站扫码登录成功，Cookie已更新，并已写入配置文件（含运行时缓存）。",
                )
                return

            if status == "expired":
                await self._send_private_text(
                    unified_msg_origin, "B站二维码已过期，本轮协助登录结束。"
                )
                return

            await self._send_private_text(
                unified_msg_origin, "B站扫码登录超时，本轮协助登录结束。"
            )
        finally:
            self._login_in_progress = False
