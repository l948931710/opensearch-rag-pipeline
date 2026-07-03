# -*- coding: utf-8 -*-
"""#F-dingtalk-webhook-whitelist 回归测试

针对 opensearch_pipeline/dingtalk_bot.py 的 _is_dingtalk_webhook：
sessionWebhook 必须 https + host 精确等于 dingtalk.com 或以 .dingtalk.com 结尾，
其余(明文 http / IP / 外域 / 后缀粘连 / 子域欺骗 / userinfo 混淆 / 空串)一律拒绝，防 SSRF。

纯函数，直接调用，无需真实 DB/SDK/浏览器。
"""
import pytest

from opensearch_pipeline.dingtalk_bot import _is_dingtalk_webhook


@pytest.mark.parametrize(
    "url, expected",
    [
        # ── 放行：钉钉官方 https 域名 ──
        ("https://oapi.dingtalk.com/robot/send?access_token=x", True),  # 官方回投地址
        ("https://api.dingtalk.com/v1.0/robot/messages/send", True),    # 新版官方域名
        ("https://dingtalk.com/", True),                                # host 精确等于主域
        # ── 拒绝：外域 ──
        ("https://evil.com/robot/send", False),
        # ── 拒绝：明文 http（即便域名合法也不放行）──
        ("http://oapi.dingtalk.com/robot/send", False),
        # ── 拒绝：内网 / 元数据 IP（SSRF 典型目标）──
        ("https://169.254.169.254/latest/meta-data/", False),
        # ── 拒绝：userinfo 混淆（urlparse 取 @ 后真实 host=evil.com）──
        ("https://oapi.dingtalk.com@evil.com/robot/send", False),
        # ── 拒绝：后缀粘连（xdingtalk.com 不是 dingtalk.com 的子域）──
        ("https://xdingtalk.com/robot/send", False),
        # ── 拒绝：子域欺骗（dingtalk.com.evil.com，真实注册域是 evil.com）──
        ("https://dingtalk.com.evil.com/robot/send", False),
        # ── 拒绝：空串（缺失 sessionWebhook）──
        ("", False),
    ],
)
def test_is_dingtalk_webhook(url, expected):
    assert _is_dingtalk_webhook(url) is expected


def test_userinfo_confusion_host_is_evil():
    """显式锁定 userinfo 混淆语义：@ 前的 oapi.dingtalk.com 只是账户名，真实 host 是 evil.com。

    #F- 修复前若用 url.startswith('https://oapi.dingtalk.com') 或对 raw url 做子串匹配，
    这条会被误判为合法钉钉地址(True)——正是本次要堵的 SSRF 缺口。
    """
    assert _is_dingtalk_webhook("https://oapi.dingtalk.com@169.254.169.254/") is False


def test_scheme_case_insensitive_https():
    """scheme 大小写不敏感：HTTPS 也应视为 https 放行。"""
    assert _is_dingtalk_webhook("HTTPS://oapi.dingtalk.com/robot/send") is True
