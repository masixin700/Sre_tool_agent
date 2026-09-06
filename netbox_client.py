# -*- coding: utf-8 -*-
"""NetBox REST 客户端（零依赖，标准库 urllib 实现）。

- 启动时用账号密码调用 /api/users/tokens/provision/ 自动获取 API Token；
- 封装 GET / POST / PATCH / DELETE，全部返回解析后的 JSON。
"""
import json
import logging
import urllib.parse
import urllib.request
import urllib.error

import config

log = logging.getLogger("netbox")


class NetBoxError(RuntimeError):
    pass


class NetBoxClient:
    def __init__(self, base_url=config.NETBOX_URL, user=config.NETBOX_USER,
                 password=config.NETBOX_PASSWORD):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.token = None

    # ---------- 认证 ----------
    def login(self):
        """用账号密码换取 API Token（v1 兼容格式，Authorization: Token <key>）。"""
        resp = self._raw("POST", "/api/users/tokens/provision/",
                         body={"username": self.user, "password": self.password, "version": 1},
                         auth=False)
        self.token = resp.get("token")
        if not self.token:
            raise NetBoxError("Provision 接口未返回 token: %s" % resp)
        log.info("NetBox 登录成功，token=%s...", self.token[:6])
        return self.token

    # ---------- 基础 HTTP ----------
    def _raw(self, method, path, body=None, params=None, auth=True):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                self.login()
            headers["Authorization"] = "Token " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise NetBoxError("HTTP %s %s -> %s %s" % (method, path, e.code, detail[:500]))

    def get(self, path, **params):
        return self._raw("GET", path, params=params or None)

    def post(self, path, body):
        return self._raw("POST", path, body=body)

    def patch(self, path, body):
        return self._raw("PATCH", path, body=body)

    def delete(self, path):
        return self._raw("DELETE", path)

    # ---------- 便捷分页查询 ----------
    def get_all(self, path, **params):
        """拉取全部分页结果。"""
        results, offset = [], 0
        while True:
            page = self.get(path, limit=200, offset=offset, **params)
            results.extend(page.get("results", []))
            if not page.get("next"):
                return results
            offset += 200


# 全局单例
nb = NetBoxClient()
