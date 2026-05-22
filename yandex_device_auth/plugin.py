"""
Yandex Device Auth Plugin

Yandex QR-code authentication (magic_x_token):
- User scans QR on phone → confirms in Yandex app
- Backend gets x_token (super-token, 1 year validity)
- x_token used for Quasar API calls

Архитектура: UI не знает провайдера/OAuth. Плагин пишет состояние в
storage ("inspector"/"auth_flows"); Inspector читает; UI только
GET /admin/v1/inspector/auth и POST /admin/v1/operations.

HTTP API (legacy/прямые вызовы):
- POST /api/v1/plugins/yandex-device-auth/device/start — generate QR URL
- GET /api/v1/plugins/yandex-device-auth/device/status — check QR confirmation status
- POST /api/v1/plugins/yandex-device-auth/device/cookies — manual cookie submission
- GET /api/v1/plugins/yandex-device-auth/device/session — get account session status
- POST /api/v1/plugins/yandex-device-auth/device/unlink — unlink account

Events:
- yandex.device_auth.linked — account linked
- yandex.device_auth.unlinked — account unlinked

Storage:
- yandex/device_auth/session — account metadata with x_token
"""
from typing import Any, Dict, List, Optional

from sdk.plugin_ext import BasePlugin, PluginMetadata
from sdk import ServiceAuthConfig
from .device_auth_service import YandexDeviceAuthService

FLOW_ID_YANDEX_DEVICE = "yandex-device"
AUTH_INSPECTOR_STORAGE_NS = "inspector"
AUTH_INSPECTOR_STORAGE_KEY = "auth_flows"


class YandexDeviceAuthPlugin(BasePlugin):
    """Yandex Device Authentication Plugin."""

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="yandex_device_auth",
            version="1.0.0",
            description="Yandex QR-code and password authentication for Quasar",
            author="Home Console",
            capabilities_provided=["yandex:session_cookies"],
        )

    async def _sync_auth_inspector_flows(
        self, extra_flow_data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Обновить инспекторные auth-flows в storage (SDK-first).

        UI читает только Inspector, не знает провайдера.
        extra_flow_data: при старте QR — сюда передать result с qr_url/qr_svg для отображения в UI."""
        try:
            account = await self.auth_service.get_account_status()
            has_pending = getattr(
                self.auth_service, "_yandex_session", None
            ) is not None

            if account.get("linked"):
                state = "authorized"
                message = "Аккаунт привязан."
                actions = [
                    {"type": "yandex.sync_devices", "label": "Синхронизировать устройства", "params": {}},
                    {"type": "yandex_device_auth.unlink", "label": "Отвязать", "params": {}},
                ]
            elif has_pending:
                state = "pending_code"
                message = "Отсканируйте QR или введите код на странице авторизации. Затем нажмите «Проверить статус»."
                actions = [
                    {"type": "yandex_device_auth.status", "label": "Проверить статус", "params": {}},
                    {"type": "yandex_device_auth.cancel", "label": "Отмена", "params": {}},
                ]
            else:
                state = "not_started"
                message = None
                actions = [
                    {"type": "yandex_device_auth.start", "label": "Начать авторизацию", "params": {}},
                ]

            flow = {
                "id": FLOW_ID_YANDEX_DEVICE,
                "state": state,
                "actions": actions,
            }
            if message is not None:
                flow["message"] = message
            # QR для отображения во Flutter: при pending_code передаём qr_url и qr_svg из результата start
            if state == "pending_code" and extra_flow_data and isinstance(extra_flow_data, dict):
                if extra_flow_data.get("qr_url"):
                    flow["qr_url"] = extra_flow_data["qr_url"]
                if extra_flow_data.get("qr_svg"):
                    flow["qr_svg"] = extra_flow_data["qr_svg"]

            raw = await self.storage_get(AUTH_INSPECTOR_STORAGE_NS, AUTH_INSPECTOR_STORAGE_KEY)
            flows: List[Dict[str, Any]] = list(raw) if isinstance(raw, list) else []
            flows = [f for f in flows if isinstance(f, dict) and f.get("id") != FLOW_ID_YANDEX_DEVICE]
            flows.append(flow)
            await self.storage_set(AUTH_INSPECTOR_STORAGE_NS, AUTH_INSPECTOR_STORAGE_KEY, flows)
        except Exception:
            pass

    async def on_load(self) -> None:
        """Register services and HTTP endpoints."""
        await super().on_load()

        # Передаём self (BasePlugin) как SDK-first facade: call_service/storage_*/publish_event/has_service.
        self.auth_service = YandexDeviceAuthService(self)

        async def start_device_auth(
            *, body: Optional[Dict[str, Any]] = None, method: Optional[str] = None, **kwargs
        ) -> Dict[str, Any]:
            """Start QR, cookies, password, or token authorization.

            Args:
                method: "qr", "cookies", "password", "token"
                body: дополнительные параметры:
                    - для "cookies": {"cookies": "..."}
                    - для "password": {"username": "...", "password": "..."}
                    - для "token": {"token": "..."}

            Returns:
                {
                    "qr_url": str (для method="qr"),
                    "oauth_url": str (alias для qr_url),
                    "status": str,
                    "method": str,
                }
            """
            if body and isinstance(body, dict):
                method = body.get("method") or method

            method = method or "qr"

            try:
                result = await self.auth_service.start_auth(method, body)
                await self._sync_auth_inspector_flows(extra_flow_data=result)
                return result
            except Exception as e:
                await self._sync_auth_inspector_flows()
                msg = str(e)
                if "csrf_token" in msg:
                    msg = (
                        "Yandex upstream недоступен/изменил страницу авторизации (csrf_token не найден). "
                        "Попробуйте позже или используйте ручную передачу cookies."
                    )
                # Важно: не падать 500 на публичном endpoint — UI ожидает структурированный ответ.
                return {"ok": False, "error": msg, "code": "UNAVAILABLE"}

        async def check_qr_status(
            *, body: Optional[Dict[str, Any]] = None, **kwargs
        ) -> Dict[str, Any]:
            """Check if user confirmed QR authorization.

            Returns:
                {
                    "status": "approved" | "pending",
                    "quasar_ready": bool,
                    "x_token": str (if approved),
                }
            """
            result = await self.auth_service.check_qr_status()
            await self._sync_auth_inspector_flows()
            if result:
                return result
            return {"status": "pending"}

        async def save_cookies(
            *, body: Optional[Dict[str, Any]] = None, cookies: Optional[str] = None, **kwargs
        ) -> Dict[str, Any]:
            """Save cookies from manual submission.

            Args:
                cookies: JSON array or raw cookie string

            Returns:
                {"status": "linked", "quasar_ready": true}
            """
            if body and isinstance(body, dict):
                cookies = body.get("cookies") or cookies

            if not cookies:
                raise ValueError("cookies are required")

            result = await self.auth_service.save_cookies(cookies)
            await self._sync_auth_inspector_flows()
            return result

        async def get_account_status(
            *, body: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, Any]] = None, **kwargs
        ) -> Dict[str, Any]:
            """Get account status.

            Returns:
                {"linked": bool, "quasar_ready": bool, "linked_at": float, "method": str}
            """
            return await self.auth_service.get_account_status()

        async def cancel_auth(
            *, body: Optional[Dict[str, Any]] = None, **kwargs
        ) -> Dict[str, Any]:
            """Cancel ongoing authorization (alias for unlink for compatibility).

            Returns:
                {"status": "cancelled"}
            """
            # Отменяем активную сессию авторизации
            if hasattr(self.auth_service, '_yandex_session') and self.auth_service._yandex_session:
                if self.auth_service._yandex_session._session:
                    await self.auth_service._yandex_session._session.close()
                self.auth_service._yandex_session = None
                self.auth_service._auth_method = None

            await self._sync_auth_inspector_flows()
            return {"status": "cancelled"}

        async def unlink_account(
            *, body: Optional[Dict[str, Any]] = None, **kwargs
        ) -> Dict[str, Any]:
            """Unlink account and clear session.

            Returns:
                {"status": "unlinked"}
            """
            result = await self.auth_service.unlink_account()
            await self._sync_auth_inspector_flows()
            return result

        async def get_account_session() -> Dict[str, Any]:
            """Get current account session status.

            Returns:
                {
                    "linked": bool,
                    "quasar_ready": bool,
                    "linked_at": float,
                    "method": str,
                }
            """
            return await self.auth_service.get_account_status()

        # Register services.
        #
        # Access control is enforced declaratively via ServiceAuthConfig (SDK-first):
        # - yandex_device_auth.*: require integrations.yandex.*
        # - device_auth.*: public (legacy direct HTTP surface used before login)
        yandex_scoped = ServiceAuthConfig(public=False, required_scopes=["integrations.yandex.*"])
        device_public = ServiceAuthConfig(public=True)
        await self.register_service(
            "yandex_device_auth.start",
            start_device_auth,
            admin_only=False,
            auth_config=yandex_scoped,
        )
        await self.register_service(
            "device_auth.start",
            start_device_auth,
            admin_only=False,
            auth_config=device_public,
        )
        await self.register_service(
            "yandex_device_auth.status",
            check_qr_status,
            admin_only=False,
            auth_config=yandex_scoped,
        )
        await self.register_service(
            "device_auth.status",
            check_qr_status,
            admin_only=False,
            auth_config=device_public,
        )
        await self.register_service(
            "yandex_device_auth.cookies",
            save_cookies,
            admin_only=False,
            auth_config=yandex_scoped,
        )
        await self.register_service(
            "device_auth.cookies",
            save_cookies,
            admin_only=False,
            auth_config=device_public,
        )
        await self.register_service(
            "yandex_device_auth.get_account_status",
            get_account_status,
            admin_only=False,
            auth_config=yandex_scoped,
        )
        await self.register_service(
            "device_auth.get_account_status",
            get_account_status,
            admin_only=False,
            auth_config=device_public,
        )
        await self.register_service(
            "yandex_device_auth.cancel",
            cancel_auth,
            admin_only=False,
            auth_config=yandex_scoped,
        )
        await self.register_service(
            "device_auth.cancel",
            cancel_auth,
            admin_only=False,
            auth_config=device_public,
        )
        await self.register_service(
            "yandex_device_auth.unlink",
            unlink_account,
            admin_only=False,
            auth_config=yandex_scoped,
        )
        await self.register_service(
            "device_auth.unlink",
            unlink_account,
            admin_only=False,
            auth_config=device_public,
        )
        await self.register_service(
            "yandex_device_auth.get_session",
            get_account_session,
            admin_only=False,
            auth_config=yandex_scoped,
        )
        await self.register_service(
            "device_auth.get_session",
            get_account_session,
            admin_only=False,
            auth_config=device_public,
        )

        # Register HTTP endpoints
        from sdk.http import HttpEndpoint
        from sdk.http import EndpointAuthConfig

        try:
            self.register_http_endpoint(
                HttpEndpoint(
                    method="POST",
                    path="/api/v1/plugins/yandex-device-auth/device/start",
                    service="device_auth.start",
                    description="Start QR or password authorization",
                    auth_config=EndpointAuthConfig(public=True),
                )
            )

            self.register_http_endpoint(
                HttpEndpoint(
                    method="GET",
                    path="/api/v1/plugins/yandex-device-auth/device/status",
                    service="device_auth.status",
                    description="Check QR confirmation status",
                    auth_config=EndpointAuthConfig(public=True),
                )
            )

            self.register_http_endpoint(
                HttpEndpoint(
                    method="POST",
                    path="/api/v1/plugins/yandex-device-auth/device/cookies",
                    service="device_auth.cookies",
                    description="Save cookies from manual submission",
                    auth_config=EndpointAuthConfig(public=True),
                )
            )

            self.register_http_endpoint(
                HttpEndpoint(
                    method="GET",
                    path="/api/v1/plugins/yandex-device-auth/device/session",
                    service="device_auth.get_session",
                    description="Get account session status",
                    auth_config=EndpointAuthConfig(public=True),
                )
            )

            self.register_http_endpoint(
                HttpEndpoint(
                    method="POST",
                    path="/api/v1/plugins/yandex-device-auth/device/cancel",
                    service="device_auth.cancel",
                    description="Cancel ongoing authorization",
                    auth_config=EndpointAuthConfig(public=True),
                )
            )

            self.register_http_endpoint(
                HttpEndpoint(
                    method="POST",
                    path="/api/v1/plugins/yandex-device-auth/device/unlink",
                    service="device_auth.unlink",
                    description="Unlink account",
                    auth_config=EndpointAuthConfig(public=True),
                )
            )
        except Exception:
            pass

        # Operation handlers: UI вызывает POST /admin/v1/operations с type из auth_flows;
        # operations.execute() ищет handler по type — регистрируем делегацию в service_registry.
        try:
            async def _op_start(params: Any, context: Any) -> Dict[str, Any]:
                result = await self.call_service(
                    "device_auth.start", body=params if isinstance(params, dict) else {}
                )
                return result if isinstance(result, dict) else {"value": result}

            async def _op_status(params: Any, context: Any) -> Dict[str, Any]:
                result = await self.call_service(
                    "device_auth.status", body=params if isinstance(params, dict) else {}
                )
                return result if isinstance(result, dict) else {"value": result}

            async def _op_cancel(params: Any, context: Any) -> Dict[str, Any]:
                result = await self.call_service(
                    "device_auth.cancel", body=params if isinstance(params, dict) else {}
                )
                return result if isinstance(result, dict) else {"value": result}

            async def _op_unlink(params: Any, context: Any) -> Dict[str, Any]:
                result = await self.call_service(
                    "device_auth.unlink", body=params if isinstance(params, dict) else {}
                )
                return result if isinstance(result, dict) else {"value": result}

            self.register_operation_handler("device_auth.start", _op_start)
            self.register_operation_handler("device_auth.status", _op_status)
            self.register_operation_handler("device_auth.cancel", _op_cancel)
            self.register_operation_handler("device_auth.unlink", _op_unlink)
        except Exception:
            # Best-effort: отсутствие operations подсистемы не должно блокировать load.
            pass

        await self._sync_auth_inspector_flows()

    async def on_unload(self) -> None:
        """Cleanup on unload."""
        await super().on_unload()

        if hasattr(self, "auth_service"):
            await self.auth_service.cleanup()

        try:
            raw = await self.storage_get(AUTH_INSPECTOR_STORAGE_NS, AUTH_INSPECTOR_STORAGE_KEY)
            flows = list(raw) if isinstance(raw, list) else []
            flows = [f for f in flows if isinstance(f, dict) and f.get("id") != FLOW_ID_YANDEX_DEVICE]
            await self.storage_set(AUTH_INSPECTOR_STORAGE_NS, AUTH_INSPECTOR_STORAGE_KEY, flows)
        except Exception:
            pass

        try:
            await self.unregister_service("yandex_device_auth.start")
            await self.unregister_service("yandex_device_auth.status")
            await self.unregister_service("yandex_device_auth.cookies")
            await self.unregister_service("yandex_device_auth.get_account_status")
            await self.unregister_service("yandex_device_auth.cancel")
            await self.unregister_service("yandex_device_auth.unlink")
            await self.unregister_service("yandex_device_auth.get_session")
            await self.unregister_service("device_auth.start")
            await self.unregister_service("device_auth.status")
            await self.unregister_service("device_auth.cookies")
            await self.unregister_service("device_auth.get_account_status")
            await self.unregister_service("device_auth.cancel")
            await self.unregister_service("device_auth.unlink")
            await self.unregister_service("device_auth.get_session")
        except Exception:
            pass
