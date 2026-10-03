"""按插件调用来源为标准网络客户端配置代理，并管理补丁与连接池生命周期。"""

from __future__ import annotations

import importlib
import sys
import threading
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.core.star.star import star_registry


@dataclass(frozen=True)
class ProxyRule:
    """一个插件的代理设置。"""

    plugin: str
    proxy: str
    client: str


class PluginProxyManager:
    """仅对调用栈中最近的目标插件应用网络代理。"""

    def __init__(self) -> None:
        self._rules: dict[str, ProxyRule] = {}
        self._patches: list[tuple[Any, str, Any, Any]] = []
        self._sync_transports: dict[Any, dict[tuple[Any, str], Any]] = {}
        self._async_transports: dict[Any, dict[tuple[Any, str], Any]] = {}
        self._reported: set[tuple[str, str]] = set()
        self._lock = threading.RLock()
        self._active = False
        self._own_prefix = __package__.rsplit(".", 1)[0]

    def install(self, entries: Any) -> None:
        """读取规则并安装已可用客户端的补丁。

        Args:
            entries: 配置面板中的插件代理规则列表。
        """
        if self._active:
            return
        if not isinstance(entries, list):
            logger.warning("插件代理配置必须是列表")
            return
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                logger.warning(f"第 {index} 条插件代理配置不是对象，已跳过")
                continue
            if not entry.get("enabled", True):
                continue
            plugin = str(entry.get("plugin") or "").strip()
            proxy = str(entry.get("proxy") or "").strip()
            client = str(entry.get("client") or "auto").strip().lower()
            try:
                parsed = urlsplit(proxy)
                valid_proxy = (
                    parsed.scheme in {"http", "https"}
                    and bool(parsed.hostname)
                    and (parsed.port is None or parsed.port > 0)
                    and parsed.path in {"", "/"}
                    and not parsed.query
                    and not parsed.fragment
                )
            except ValueError:
                valid_proxy = False
            if not plugin or not valid_proxy or client not in {"auto", "aiohttp", "httpx", "requests"}:
                logger.warning(f"第 {index} 条插件代理配置无效，请检查插件名、代理地址和客户端")
                continue
            if plugin in self._rules:
                logger.warning(f"插件 {plugin} 的代理规则重复，使用最后一条")
            self._rules[plugin] = ProxyRule(plugin, proxy, client)
        if not self._rules:
            return

        self._active = True
        required = {rule.client for rule in self._rules.values()}
        installers = {
            "aiohttp": self._install_aiohttp,
            "httpx": self._install_httpx,
            "requests": self._install_requests,
        }
        for name, installer in installers.items():
            if "auto" not in required and name not in required:
                continue
            try:
                module = importlib.import_module(name)
            except ImportError:
                logger.warning(f"网络客户端 {name} 不可用，无法代理其请求")
                continue
            installer(module)
        logger.info(f"已启用 {len(self._rules)} 条插件代理规则，客户端在实际请求时识别")

    def report_unmatched(self) -> None:
        """在插件加载完成后报告不存在或未启用的目标插件。"""
        available = {
            name
            for meta in list(star_registry)
            if meta.activated
            for name in (meta.name, meta.root_dir_name)
            if name
        }
        for plugin in self._rules:
            if plugin not in available:
                logger.warning(f"插件代理目标 {plugin} 未加载或未启用，请检查插件名称")

    def _rule_for(self, client: str) -> ProxyRule | None:
        """以最近的插件调用者为准，避免外层插件的规则影响内层插件。"""
        if not self._active:
            return None
        roots = sorted(
            (
                (meta.module_path.rsplit(".", 1)[0], meta.name, meta.root_dir_name)
                for meta in list(star_registry)
                if meta.module_path and meta.activated
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        frame = sys._getframe(1)
        try:
            while frame is not None:
                module = str(frame.f_globals.get("__name__", ""))
                if module == self._own_prefix or module.startswith(self._own_prefix + "."):
                    frame = frame.f_back
                    continue
                # 模型提供商和平台网络属于框架服务，沿用服务自身配置。
                if any(
                    module == prefix or module.startswith(prefix + ".")
                    for prefix in ("astrbot.core.provider", "astrbot.core.platform")
                ):
                    return None
                for prefix, name, directory in roots:
                    if module != prefix and not module.startswith(prefix + "."):
                        continue
                    rule = self._rules.get(name) or self._rules.get(directory)
                    if rule is None:
                        return None
                    key = (rule.plugin, client)
                    with self._lock:
                        first = key not in self._reported
                        self._reported.add(key)
                    if rule.client not in {"auto", client}:
                        if first:
                            logger.warning(f"插件 {rule.plugin} 实际使用 {client}，与指定客户端不符，本次请求不应用代理")
                        return None
                    if first:
                        logger.info(f"检测到插件 {rule.plugin} 使用 {client}，已应用代理规则")
                    return rule
                frame = frame.f_back
        finally:
            del frame
        return None

    def _patch(self, owner: Any, name: str, wrapper: Any) -> None:
        """记录原方法，卸载时仅恢复仍由本插件持有的补丁。"""
        original = getattr(owner, name)
        self._patches.append((owner, name, original, wrapper))
        setattr(owner, name, wrapper)

    def _install_aiohttp(self, module: Any) -> None:
        """在请求阶段设置代理，兼容已有会话。"""
        owner = module.ClientSession
        original = getattr(owner, "_request", None)
        if not callable(original):
            logger.warning("aiohttp 请求入口不兼容，未安装代理补丁")
            return

        @wraps(original)
        async def request(session: Any, *args: Any, **kwargs: Any) -> Any:
            rule = self._rule_for("aiohttp")
            if rule is not None:
                if type(session.connector) is not module.TCPConnector:
                    raise RuntimeError("插件代理不支持 aiohttp 自定义连接器，请使用目标插件自身的代理配置")
                kwargs.update(proxy=rule.proxy, proxy_auth=None, proxy_headers=None)
            return await original(session, *args, **kwargs)

        self._patch(owner, "_request", request)

    def _install_requests(self, module: Any) -> None:
        """在发送阶段覆盖代理，包含预构建请求。"""
        owner = module.Session
        original = owner.send

        @wraps(original)
        def send(session: Any, request: Any, **kwargs: Any) -> Any:
            rule = self._rule_for("requests")
            if rule is not None:
                if type(session.get_adapter(request.url)) is not module.adapters.HTTPAdapter:
                    raise RuntimeError("插件代理不支持 requests 自定义适配器，请使用目标插件自身的代理配置")
                kwargs["proxies"] = {"http": rule.proxy, "https": rule.proxy, "all": rule.proxy}
            return original(session, request, **kwargs)

        self._patch(owner, "send", send)

    def _install_httpx(self, module: Any) -> None:
        """为同步和异步客户端选择独立代理连接池。"""
        for owner, factory, asynchronous in (
            (module.Client, module.HTTPTransport, False),
            (module.AsyncClient, module.AsyncHTTPTransport, True),
        ):
            close_name = "aclose" if asynchronous else "close"
            exit_name = "__aexit__" if asynchronous else "__exit__"
            if not all(callable(getattr(owner, name, None)) for name in ("_transport_for_url", close_name, exit_name)):
                logger.warning("httpx 传输入口不兼容，未安装对应客户端的代理补丁")
                continue
            self._patch_httpx_route(owner, factory, asynchronous)
            for name in (close_name, exit_name):
                original = getattr(owner, name)
                self._patch(owner, name, self._httpx_close_wrapper(original, asynchronous))

    def _patch_httpx_route(self, owner: Any, factory: Any, asynchronous: bool) -> None:
        """保留客户端请求、认证与 Cookie 流程，只切换标准传输器。"""
        original = owner._transport_for_url

        @wraps(original)
        def route(client: Any, url: Any) -> Any:
            transport = original(client, url)
            rule = self._rule_for("httpx")
            if rule is None or url.scheme not in {"http", "https"}:
                return transport
            if type(transport) is not factory:
                raise RuntimeError("插件代理不支持 httpx 自定义传输器，请使用目标插件自身的代理配置")
            pools = self._async_transports if asynchronous else self._sync_transports
            key = (transport, rule.proxy)
            with self._lock:
                cache = pools.setdefault(client, {})
                if key not in cache:
                    pool = transport._pool
                    required = ("_ssl_context", "_http1", "_http2", "_max_connections", "_max_keepalive_connections", "_keepalive_expiry")
                    if not all(hasattr(pool, name) for name in required):
                        raise RuntimeError("当前 httpx/httpcore 连接池版本不兼容插件代理")
                    # 从原连接池保留 TLS、协议及连接数量设置，不改变证书验证策略。
                    httpx = sys.modules["httpx"]
                    cache[key] = factory(
                        proxy=rule.proxy,
                        verify=pool._ssl_context,
                        trust_env=False,
                        http1=pool._http1,
                        http2=pool._http2,
                        limits=httpx.Limits(
                            max_connections=pool._max_connections,
                            max_keepalive_connections=pool._max_keepalive_connections,
                            keepalive_expiry=pool._keepalive_expiry,
                        ),
                        socket_options=getattr(pool, "_socket_options", None),
                    )
                return cache[key]

        self._patch(owner, "_transport_for_url", route)

    def _pop_transports(self, client: Any, asynchronous: bool) -> list[Any]:
        """取出一个客户端的附加连接池，避免重复关闭。"""
        pools = self._async_transports if asynchronous else self._sync_transports
        with self._lock:
            return list(pools.pop(client, {}).values())

    def _httpx_close_wrapper(self, original: Any, asynchronous: bool) -> Callable[..., Any]:
        """让显式关闭和上下文退出都释放附加连接池。"""
        if asynchronous:
            @wraps(original)
            async def async_close(client: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    return await original(client, *args, **kwargs)
                finally:
                    for transport in self._pop_transports(client, True):
                        await transport.aclose()

            return async_close

        @wraps(original)
        def sync_close(client: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return original(client, *args, **kwargs)
            finally:
                for transport in self._pop_transports(client, False):
                    transport.close()

        return sync_close

    async def uninstall(self) -> None:
        """停止代理、还原方法并释放仍存在的附加连接池。"""
        self._active = False
        for owner, name, original, wrapper in reversed(self._patches):
            if getattr(owner, name) is wrapper:
                setattr(owner, name, original)
        self._patches.clear()
        for client in list(self._sync_transports):
            for transport in self._pop_transports(client, False):
                try:
                    transport.close()
                except (OSError, RuntimeError):
                    logger.warning("关闭插件代理同步连接池失败")
        for client in list(self._async_transports):
            for transport in self._pop_transports(client, True):
                try:
                    await transport.aclose()
                except (OSError, RuntimeError):
                    logger.warning("关闭插件代理异步连接池失败")
        self._rules.clear()
        self._reported.clear()
