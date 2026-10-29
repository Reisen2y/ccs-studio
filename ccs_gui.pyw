# -*- coding: utf-8 -*-
"""CCS GUI — Claude Code 供应商一键切换器（Modern Dark UI）

命令行 ccs（~/.claude/provider-switch.py）的图形外壳，内核语义保持一致：
  数据完全共用 ~/.claude/providers.json（供应商档案）与 ~/.claude/settings.json（Claude Code 配置），
  本工具与命令行 ccs 可随意混用，两边看到的永远是同一份数据。

切换语义（与 provider-switch.py 相同）：
  受管键 = 所有预设的 set+unset 键的并集 + 基础三键（+ 代理三键，防残留的超集）；
  切换时先统一清空受管键，再写入目标预设的 set，最后剔除其 unset 列表。
"""
import copy as _copy
import json
import os
import threading
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
PRESETS = os.path.join(CLAUDE_DIR, "providers.json")
PLACEHOLDER_HINT = "粘贴"

# ---------- 配色 ----------
ACCENT = "#5b6cff"        # 主色（切换按钮 / 选中边框）
ACCENT_HOVER = "#6f7dff"
GREEN = "#2fbf8f"         # 使用中徽章 / 成功
GREEN_DIM = "#1f7a5c"
PURPLE = "#8b5cf6"        # 选中（正在查看）高亮色
PURPLE_DIM = "#372d52"    # 选中卡片的底色
RED = "#e05561"
CARD = "#2b2b31"          # 卡片底色
CARD_HOVER = "#34343c"
CARD_CURRENT = "#283031"
BORDER = "#3c3c46"
BORDER_CURRENT = "#2f5d51"
TEXT_DIM = "#9a9aa4"
FONT = "Microsoft YaHei UI"
APP_VERSION = "1.0.0"

BASE_MANAGED = {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}
PROXY_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
CANONICAL_KEYS = [
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "API_TIMEOUT_MS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
]
SLOT_KEYS = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}


# ---------- 内核逻辑（与命令行 provider-switch.py 保持一致） ----------

def mask(k):
    if k and len(k) > 14:
        return f"{k[:8]}****{k[-4:]}"
    if k:
        return k[:6] + "****"
    return "(未填)"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    # 写前留一份 .bak，防止手滑改坏
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
            with open(path + ".bak", "w", encoding="utf-8") as f:
                f.write(old)
    except Exception:
        pass
    # 原子写：先写临时文件再替换，中途崩溃/断电不会留下半截 JSON
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)  # 替换失败（如目标被占用）时清掉临时文件
        except OSError:
            pass
        raise


def load_json_or_bak(path):
    """加载 JSON；主文件损坏时自动从 .bak 恢复。

    返回 (data, restored, corrupt)：
      文件不存在            -> (None, False, False)
      正常                  -> (data, False, False)
      损坏但 .bak 可用       -> (恢复的数据, True, False)
      损坏且无可用 .bak      -> (None, False, True)，同时把残骸存为 .corrupt 防止后续保存覆盖灭失
    """
    if not os.path.exists(path):
        return None, False, False
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), False, False
    except Exception:
        bak = path + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, encoding="utf-8") as f:
                    data = json.load(f)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, path)
                return data, True, False
            except Exception:
                pass
        try:
            # 保住损坏现场，避免之后任何一次保存把它覆盖掉
            with open(path, encoding="utf-8") as f:
                wreck = f.read()
            with open(path + ".corrupt", "w", encoding="utf-8") as f:
                f.write(wreck)
        except Exception:
            pass
        return None, False, True


def managed_keys(presets):
    m = set(BASE_MANAGED) | set(PROXY_KEYS)
    for p in presets.values():
        m |= set(p.get("set", {})) | set(p.get("unset", []))
    return m


def detect_current(presets, env):
    """配置指纹：与当前 settings 一致的键越多越匹配（与命令行版一致）。"""
    if not presets:
        return "(未知)"

    def score(p):
        return sum(1 for k, v in p.get("set", {}).items() if env.get(k) == v)

    name, best = max(presets.items(), key=lambda kv: score(kv[1]))
    return name if score(best) > 0 else "(未知)"


def do_switch(presets, settings, name):
    """切换内核：清空受管键 → 写入目标 set → 剔除 unset → 处理顶层 model → 落盘。"""
    p = presets[name]
    env = settings.setdefault("env", {})
    for k in managed_keys(presets):
        env.pop(k, None)
    env.update(p.get("set", {}))
    for k in p.get("unset", []):
        env.pop(k, None)
    if "model" in p:
        if p.get("model"):
            settings["model"] = p["model"]
        else:
            settings.pop("model", None)
    save_json(SETTINGS, settings)


def test_provider(p, timeout=25):
    """对预设发一个最小 /v1/messages 请求做连通性实测，返回 (ok, 消息)。"""
    s = p.get("set", {})
    base = (s.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    token = s.get("ANTHROPIC_AUTH_TOKEN") or ""
    model = (s.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
             or s.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
             or s.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or "")
    if not base or not token:
        return False, "BASE_URL 或 API Key 未填，无法测试"
    if PLACEHOLDER_HINT in token or PLACEHOLDER_HINT in model or model.startswith("【"):
        return False, "该预设的 Key/模型还是占位符，请先在「编辑」里填真实值"
    url = base + "/v1/messages"
    body = json.dumps({
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "Authorization": "Bearer " + token,
        "x-api-key": token,
    }
    proxy = s.get("HTTPS_PROXY") or s.get("HTTP_PROXY")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            return True, f"HTTP {r.status} · 模型 {data.get('model', '?')} · 响应正常"
    except urllib.error.HTTPError as e:
        snippet = e.read().decode("utf-8", "replace")[:220].replace("\n", " ")
        return False, f"HTTP {e.code}: {snippet}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------- 供应商卡片 ----------

class ProviderCard(ctk.CTkFrame):
    """列表里的一张预设卡片。单击/选中时卡片本身不做任何重绘（列表绝对静止），
    选中状态只体现在右侧详情面板；仅「使用中」状态变化（切换供应商）才更新卡片。"""

    CARD_H = 78  # 卡片固定高度（像素），永不变

    def __init__(self, master, app, name, preset, is_current):
        super().__init__(master, height=ProviderCard.CARD_H,
                         fg_color=CARD_CURRENT if is_current else CARD,
                         corner_radius=12, border_width=2,
                         border_color=BORDER_CURRENT if is_current else BORDER)
        self.app = app
        self.name = name
        self.is_current = bool(is_current)
        self._selected = False
        self.pack_propagate(False)  # 关键：内容自动测量再抽风也改变不了卡片尺寸
        s = preset.get("set", {})
        model = (preset.get("actual")
                 or s.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                 or s.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
                 or s.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or "?")

        top = ctk.CTkFrame(self, fg_color="transparent", height=30)
        top.pack(fill="x", padx=12, pady=(8, 0))
        top.pack_propagate(False)
        self.title_label = ctk.CTkLabel(top, text=f"●  {name}" if is_current else name,
                                        font=ctk.CTkFont(FONT, 14, "bold"), height=28,
                                        text_color=GREEN if is_current else "#eaeaea")
        self.title_label.pack(side="left")
        self.badge = ctk.CTkLabel(top, text=" 使用中 ", font=ctk.CTkFont(FONT, 11), height=22,
                                  text_color=GREEN, fg_color=GREEN_DIM, corner_radius=6)
        if is_current:
            self.badge.pack(side="left", padx=8)
        ctk.CTkLabel(top, text=mask(s.get("ANTHROPIC_AUTH_TOKEN", "")),
                     font=ctk.CTkFont("Consolas", 11), height=22, text_color=TEXT_DIM
                     ).pack(side="right")

        url = s.get("ANTHROPIC_BASE_URL", "")
        sub = model if len(model) <= 46 else model[:45] + "…"
        if url:
            sub = f"{sub}   ·   {url if len(url) <= 40 else url[:39] + '…'}"
        ctk.CTkLabel(self, text=sub, anchor="w", justify="left", height=26, wraplength=520,
                     font=ctk.CTkFont(FONT, 11), text_color=TEXT_DIM
                     ).pack(fill="x", padx=12, pady=(2, 0))

        self._bind_click_recursive(self, app, name)

    def set_current(self, is_current):
        """仅切换供应商时调用（每次最多两张卡）。"""
        self.is_current = bool(is_current)
        self.configure(fg_color=CARD_CURRENT if self.is_current else CARD,
                       border_color=BORDER_CURRENT if self.is_current else BORDER)
        self.title_label.configure(
            text=f"●  {self.name}" if self.is_current else self.name,
            text_color=GREEN if self.is_current else "#eaeaea")
        if self.is_current:
            self.badge.pack(side="left", padx=8)
        else:
            self.badge.pack_forget()

    def set_selected(self, selected):
        """点击查看时以紫色标出。只改颜色，边框宽度恒为 2、几何固定，不会抖动。"""
        self._selected = bool(selected)
        self.configure(
            fg_color=PURPLE_DIM if self._selected else (CARD_CURRENT if self.is_current else CARD),
            border_color=PURPLE if self._selected else (BORDER_CURRENT if self.is_current else BORDER))
        self.title_label.configure(
            text_color="#d9ccff" if self._selected else (GREEN if self.is_current else "#eaeaea"))

    @staticmethod
    def _bind_click_recursive(widget, app, name):
        # 每层只绑一个目标（优先内部画布）。此前 widget 和 _canvas 各绑一次会导致
        # 同一次点击触发 2~3 遍回调，是抖动放大的元凶之一。
        for seq, cb in (("<Button-1>", lambda e: app.select_provider(name)),
                        ("<Double-1>", lambda e: app.switch_to(name))):
            target = getattr(widget, "_canvas", None)
            if target is None:
                target = widget
            try:
                target.bind(seq, cb)
            except Exception:
                pass
        for child in widget.winfo_children():
            ProviderCard._bind_click_recursive(child, app, name)

    def set_highlight(self, selected):
        if selected:
            self.configure(border_color=ACCENT, border_width=2, fg_color=CARD_HOVER)
        else:
            is_cur = self.app.cur_name == self.name
            self.configure(border_color=BORDER_CURRENT if is_cur else BORDER,
                           border_width=1,
                           fg_color=CARD_CURRENT if is_cur else CARD)


# ---------- 编辑/新建弹窗 ----------

class ProviderDialog(ctk.CTkToplevel):
    def __init__(self, master, preset=None, name="", on_save=None):
        super().__init__(master, fg_color="#202024")
        self.title("编辑供应商" if name else "新建供应商")
        self.geometry("640x600")
        self.minsize(600, 540)
        self.resizable(True, True)
        self.on_save = on_save
        self.orig_name = name
        self.orig_set = dict((preset or {}).get("set", {}))
        s = self.orig_set

        slot_vals = [s.get(k, "") for k in SLOT_KEYS.values()]
        same_model = slot_vals[0] != "" and len(set(slot_vals)) == 1

        self.vars = {
            "name": tk.StringVar(value=name),
            "comment": tk.StringVar(value=(preset or {}).get("comment", "")),
            "url": tk.StringVar(value=s.get("ANTHROPIC_BASE_URL", "")),
            "key": tk.StringVar(value=s.get("ANTHROPIC_AUTH_TOKEN", "")),
            "model": tk.StringVar(value=slot_vals[0] if same_model else ""),
            "opus": tk.StringVar(value=s.get(SLOT_KEYS["opus"], "")),
            "sonnet": tk.StringVar(value=s.get(SLOT_KEYS["sonnet"], "")),
            "haiku": tk.StringVar(value=s.get(SLOT_KEYS["haiku"], "")),
            "topmodel": tk.StringVar(value=str((preset or {}).get("model") or "")),
            "effort": tk.StringVar(value=s.get("CLAUDE_CODE_EFFORT_LEVEL", "")),
            "timeout": tk.StringVar(value=s.get("API_TIMEOUT_MS", "")),
            "compact": tk.StringVar(value=s.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "")),
            "httpproxy": tk.StringVar(value=s.get("HTTP_PROXY", "")),
            "httpsproxy": tk.StringVar(value=s.get("HTTPS_PROXY", "")),
            "actual": tk.StringVar(value=(preset or {}).get("actual", "")),
        }
        self.show_key = tk.BooleanVar(value=False)
        self.key_entry = None
        # 三槽各不相同时自动展开高级区，避免简单框覆盖不掉也看不到
        self.adv_on = tk.BooleanVar(value=len(set(slot_vals)) > 1)

        sf = ctk.CTkScrollableFrame(self, fg_color="transparent")
        sf.pack(fill="both", expand=True, padx=6, pady=6)

        # —— 常用区：只露 5 个框，和右侧详情面板同一组信息 ——
        frm = ctk.CTkFrame(sf, fg_color="transparent")
        frm.pack(fill="x", padx=10, pady=(4, 0))
        frm.columnconfigure(1, weight=1)
        row = [0]

        def lab(text):
            return ctk.CTkLabel(frm, text=text, anchor="e", text_color=TEXT_DIM,
                                font=ctk.CTkFont(FONT, 12))

        def add(label, widget, colspan=1, tip=""):
            lab(label).grid(row=row[0], column=0, sticky="e", padx=(0, 10), pady=5)
            widget.grid(row=row[0], column=1, columnspan=colspan, sticky="we", pady=5)
            if tip:
                ctk.CTkLabel(frm, text=tip, text_color="#6d6d76", anchor="w",
                             font=ctk.CTkFont(FONT, 10)).grid(
                    row=row[0], column=2, sticky="w", padx=(10, 0), pady=5)
            row[0] += 1

        def entry(var, width=240, placeholder=""):
            return ctk.CTkEntry(frm, textvariable=var, width=width,
                                placeholder_text=placeholder,
                                fg_color=CARD, border_color=BORDER)

        add("名称", entry(self.vars["name"], 220, "如 dddai-grok"), tip="切换用的名字")
        add("说明", entry(self.vars["comment"], 340), tip="这家中转/官方的备注")
        add("BASE_URL", entry(self.vars["url"], 340, "https://api.example.com"))

        keyf = ctk.CTkFrame(frm, fg_color="transparent")
        self.key_entry = ctk.CTkEntry(keyf, textvariable=self.vars["key"], width=250,
                                      show="*", placeholder_text="sk-…",
                                      fg_color=CARD, border_color=BORDER)
        self.key_entry.pack(side="left")
        ctk.CTkSwitch(keyf, text="显示", variable=self.show_key, width=60,
                      command=self._toggle_key, font=ctk.CTkFont(FONT, 11)).pack(side="left", padx=12)
        add("API Key", keyf)

        add("模型名", entry(self.vars["model"], 340, "如 glm-4.7-flash"),
            tip="Opus/Sonnet/Haiku 三个槽位统一用它")
        ctk.CTkLabel(frm, text="留空 = 三槽保持不变", anchor="w", text_color="#6d6d76",
                     font=ctk.CTkFont(FONT, 10)).grid(row=row[0], column=1, sticky="w")
        row[0] += 1

        # —— 高级开关 + 折叠区 ——
        advbar = ctk.CTkFrame(sf, fg_color="transparent")
        advbar.pack(fill="x", padx=10, pady=(12, 0))
        ctk.CTkCheckBox(advbar, text="高级选项（三槽分设 / 档位 / 超时 / 代理 / 环境变量）",
                        variable=self.adv_on, command=self._toggle_adv,
                        font=ctk.CTkFont(FONT, 12), checkbox_width=18, checkbox_height=18
                        ).pack(anchor="w")

        self.adv = ctk.CTkFrame(sf, fg_color="transparent")
        afrm = ctk.CTkFrame(self.adv, fg_color="transparent")
        afrm.pack(fill="x", padx=10, pady=(4, 0))
        afrm.columnconfigure(1, weight=1)
        arow = [0]

        def alab(text):
            return ctk.CTkLabel(afrm, text=text, anchor="e", text_color=TEXT_DIM,
                                font=ctk.CTkFont(FONT, 12))

        def aadd(label, widget, colspan=1, tip=""):
            alab(label).grid(row=arow[0], column=0, sticky="e", padx=(0, 10), pady=5)
            widget.grid(row=arow[0], column=1, columnspan=colspan, sticky="we", pady=5)
            if tip:
                ctk.CTkLabel(afrm, text=tip, text_color="#6d6d76", anchor="w",
                             font=ctk.CTkFont(FONT, 10)).grid(
                    row=arow[0], column=2, sticky="w", padx=(10, 0), pady=5)
            arow[0] += 1

        def aentry(var, width=240, placeholder=""):
            return ctk.CTkEntry(afrm, textvariable=var, width=width,
                                placeholder_text=placeholder,
                                fg_color=CARD, border_color=BORDER)

        slotf = ctk.CTkFrame(afrm, fg_color="transparent")
        for i, (k, caption) in enumerate((("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"))):
            ctk.CTkLabel(slotf, text=caption, text_color=TEXT_DIM,
                         font=ctk.CTkFont(FONT, 11)).grid(row=0, column=i * 2, padx=(0, 5))
            ctk.CTkEntry(slotf, textvariable=self.vars[k], width=130,
                         fg_color=CARD, border_color=BORDER).grid(row=0, column=i * 2 + 1, padx=(0, 10))
        ctk.CTkButton(slotf, text="三槽统一", width=76, height=28,
                      fg_color=CARD, hover_color=CARD_HOVER, border_width=1,
                      border_color=BORDER, font=ctk.CTkFont(FONT, 11),
                      command=self._unify_slots).grid(row=0, column=6)
        aadd("模型槽位", slotf, tip="三个槽位各映射到哪个真实模型")

        aadd("顶层 model", aentry(self.vars["topmodel"], 200), tip="留空 = 不指定")
        aadd("思考档位", ctk.CTkComboBox(afrm, values=["", "max", "high", "medium", "low"],
                                         variable=self.vars["effort"], width=120,
                                         fg_color=CARD, border_color=BORDER,
                                         button_color=BORDER, button_hover_color=CARD_HOVER),
             tip="CLAUDE_CODE_EFFORT_LEVEL")
        aadd("超时 ms", aentry(self.vars["timeout"], 140, "600000"), tip="API_TIMEOUT_MS")
        aadd("压缩窗口", aentry(self.vars["compact"], 140, "1000000"), tip="AUTO_COMPACT_WINDOW")

        proxf = ctk.CTkFrame(afrm, fg_color="transparent")
        ctk.CTkLabel(proxf, text="HTTP", text_color=TEXT_DIM,
                     font=ctk.CTkFont(FONT, 11)).grid(row=0, column=0, padx=(0, 5))
        ctk.CTkEntry(proxf, textvariable=self.vars["httpproxy"], width=150,
                     placeholder_text="127.0.0.1:7897",
                     fg_color=CARD, border_color=BORDER).grid(row=0, column=1, padx=(0, 12))
        ctk.CTkLabel(proxf, text="HTTPS", text_color=TEXT_DIM,
                     font=ctk.CTkFont(FONT, 11)).grid(row=0, column=2, padx=(0, 5))
        ctk.CTkEntry(proxf, textvariable=self.vars["httpsproxy"], width=150,
                     placeholder_text="127.0.0.1:7897",
                     fg_color=CARD, border_color=BORDER).grid(row=0, column=3)
        aadd("代理", proxf, tip="留空不启用")

        aadd("实际模型备注", aentry(self.vars["actual"], 340), tip="给未来自己看的实测记录")

        ctk.CTkLabel(afrm, text="额外环境变量", anchor="e", text_color=TEXT_DIM,
                     font=ctk.CTkFont(FONT, 12)).grid(row=arow[0], column=0, sticky="ne",
                                                      padx=(0, 10), pady=5)
        self.extra = ctk.CTkTextbox(afrm, height=92, fg_color=CARD, border_width=1,
                                    border_color=BORDER, font=ctk.CTkFont("Consolas", 12))
        self.extra.grid(row=arow[0], column=1, columnspan=2, sticky="we", pady=5)
        arow[0] += 1
        mapped = ({"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"} | set(SLOT_KEYS.values()) |
                  {"CLAUDE_CODE_EFFORT_LEVEL", "API_TIMEOUT_MS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                   "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})
        extra_keys = [k for k in s if k not in mapped]
        self.extra.insert("1.0", "\n".join(f"{k}={s[k]}" for k in extra_keys))
        ctk.CTkLabel(afrm, text="每行一条 KEY=VALUE，如 ANTHROPIC_MODEL=deepseek-v4-pro[1m]",
                     anchor="w", text_color="#6d6d76",
                     font=ctk.CTkFont(FONT, 10)).grid(row=arow[0], column=1, sticky="w")

        # —— 保存 / 取消 ——
        self.btnf = ctk.CTkFrame(sf, fg_color="transparent")
        self.btnf.pack(fill="x", padx=10, pady=(16, 6))
        ctk.CTkButton(self.btnf, text="保 存", width=140, height=36, corner_radius=8,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      font=ctk.CTkFont(FONT, 13, "bold"), command=self._save).pack(side="left", padx=8)
        ctk.CTkButton(self.btnf, text="取 消", width=110, height=36, corner_radius=8,
                      fg_color=CARD, hover_color=CARD_HOVER, border_width=1,
                      border_color=BORDER, font=ctk.CTkFont(FONT, 13),
                      command=self.destroy).pack(side="left", padx=8)

        self._toggle_adv()
        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(220, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass

    def _toggle_adv(self):
        if self.adv_on.get():
            self.adv.pack(fill="x", before=self.btnf)
        else:
            self.adv.pack_forget()

    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "*")

    def _unify_slots(self):
        v = self.vars["opus"].get().strip() or self.vars["sonnet"].get().strip()
        if not v:
            messagebox.showwarning("三槽统一", "请先在 Opus 槽里填模型名", parent=self)
            return
        self.vars["opus"].set(v)
        self.vars["sonnet"].set(v)
        self.vars["haiku"].set(v)

    def _save(self):
        name = self.vars["name"].get().strip()
        url = self.vars["url"].get().strip()
        if not name:
            messagebox.showwarning("缺少名称", "请填写预设名称", parent=self)
            return
        if name != self.orig_name and name in load_json(PRESETS, {}):
            messagebox.showwarning("名称冲突", f"已存在名为 {name} 的预设", parent=self)
            return
        if not url:
            messagebox.showwarning("缺少 URL", "请填写 BASE_URL", parent=self)
            return
        setd = {"ANTHROPIC_BASE_URL": url}
        if self.vars["key"].get().strip():
            setd["ANTHROPIC_AUTH_TOKEN"] = self.vars["key"].get().strip()
        if self.adv_on.get():
            # 高级模式：三槽各自取值
            for k, key in SLOT_KEYS.items():
                if self.vars[k].get().strip():
                    setd[key] = self.vars[k].get().strip()
        else:
            # 简单模式：模型名统一填三槽；留空则沿用原有槽位（编辑时不丢数据）
            model = self.vars["model"].get().strip()
            if model:
                for key in SLOT_KEYS.values():
                    setd[key] = model
            else:
                for key in SLOT_KEYS.values():
                    if key in self.orig_set:
                        setd[key] = self.orig_set[key]
        if self.vars["effort"].get().strip():
            setd["CLAUDE_CODE_EFFORT_LEVEL"] = self.vars["effort"].get().strip()
        if self.vars["timeout"].get().strip():
            setd["API_TIMEOUT_MS"] = self.vars["timeout"].get().strip()
        if self.vars["compact"].get().strip():
            setd["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = self.vars["compact"].get().strip()
        hp, hsp = self.vars["httpproxy"].get().strip(), self.vars["httpsproxy"].get().strip()
        if hp:
            setd["HTTP_PROXY"] = hp
        if hsp:
            setd["HTTPS_PROXY"] = hsp
        if hp or hsp:
            setd.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        for line in self.extra.get("1.0", "end").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                messagebox.showwarning("格式错误", f"额外环境变量这一行没有 '='：\n{line}", parent=self)
                return
            k, v = line.split("=", 1)
            if k.strip():
                setd[k.strip()] = v.strip()

        if self.adv_on.get():
            actual = self.vars["actual"].get().strip()
        else:
            # 简单模式：备注留空时自动用模型名补上，详情面板的「实际模型」才好看
            actual = self.vars["actual"].get().strip() or self.vars["model"].get().strip()

        preset = {
            "comment": self.vars["comment"].get().strip(),
            "actual": actual,
            "set": setd,
            "unset": [k for k in CANONICAL_KEYS if k not in setd],
            "model": self.vars["topmodel"].get().strip() or None,
        }
        if self.on_save:
            self.on_save(name, preset, self.orig_name)
        self.destroy()


# ---------- 主窗口 ----------

class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color="#1b1b1f")
        self.title(f"CCS · Claude Code 供应商切换器 v{APP_VERSION}")
        self.geometry("1060x800")
        # 窗口整体冻结：Tk 根窗口默认会随内容尺寸自适应，这正是每次点击
        # 「左右挤/上下跳」的总开关。冻结后任何内容变化都无法改变窗口大小。
        self.resizable(False, False)
        self.grid_propagate(False)

        self.presets = {}
        self.settings = {}
        self.cur_name = "(未知)"
        self.selected_name = None
        self._prev_selected = None
        self._detail_active = False
        self.cards = {}
        self._test_running = False

        self.grid_columnconfigure(0, weight=11)
        self.grid_columnconfigure(1, weight=9)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_status_card()
        self._build_list_panel()
        self._build_detail_panel()
        self._build_statusbar()
        self.reload()

    # ----- 布局 -----

    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 4))
        ctk.CTkLabel(head, text="⚡ CCS 供应商切换器",
                     font=ctk.CTkFont(FONT, 20, "bold"), anchor="w").pack(side="left")
        ctk.CTkLabel(head, text="命令行 ccs 的图形外壳 · 数据实时共用",
                     font=ctk.CTkFont(FONT, 11), text_color=TEXT_DIM).pack(side="left", padx=12, pady=(6, 0))
        ctk.CTkButton(head, text="⟳ 刷新", width=86, height=30, corner_radius=8,
                      fg_color=CARD, hover_color=CARD_HOVER, border_width=1,
                      border_color=BORDER, font=ctk.CTkFont(FONT, 12),
                      command=self.reload).pack(side="right")

    def _build_status_card(self):
        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14,
                            border_width=1, border_color=BORDER)
        card.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 10))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=12)
        self.lbl_cur = ctk.CTkLabel(inner, font=ctk.CTkFont(FONT, 15, "bold"), height=30,
                                    wraplength=920, text_color=GREEN, anchor="w")
        self.lbl_cur.pack(anchor="w", fill="x")
        self.lbl_url = ctk.CTkLabel(inner, font=ctk.CTkFont(FONT, 12), height=26,
                                    wraplength=920, text_color=TEXT_DIM, anchor="w")
        self.lbl_url.pack(anchor="w", pady=(2, 0), fill="x")
        self.lbl_actual = ctk.CTkLabel(inner, font=ctk.CTkFont(FONT, 12), height=44,
                                       text_color=TEXT_DIM, anchor="w", wraplength=900,
                                       justify="left")
        self.lbl_actual.pack(anchor="w", pady=(2, 0), fill="x")

    def _build_list_panel(self):
        panel = ctk.CTkFrame(self, fg_color="transparent")
        panel.grid(row=2, column=0, sticky="nsew", padx=(18, 6), pady=2)
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(panel, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.lbl_count = ctk.CTkLabel(bar, text="供应商预设", font=ctk.CTkFont(FONT, 13, "bold"),
                                      anchor="w")
        self.lbl_count.pack(side="left")
        ctk.CTkLabel(bar, text="单击选中 · 双击快速切换", font=ctk.CTkFont(FONT, 10),
                     text_color="#6d6d76").pack(side="left", padx=10, pady=(3, 0))
        ctk.CTkButton(bar, text="＋ 新建", width=76, height=28, corner_radius=8,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      font=ctk.CTkFont(FONT, 12, "bold"),
                      command=self.add_provider).pack(side="right")

        self.scroller = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        self.scroller.grid(row=1, column=0, sticky="nsew")

    def _build_detail_panel(self):
        panel = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14,
                             border_width=1, border_color=BORDER)
        panel.grid(row=2, column=1, sticky="nsew", padx=(6, 18), pady=2)
        panel.rowconfigure(1, weight=1)
        panel.columnconfigure(0, weight=1)

        head = ctk.CTkFrame(panel, fg_color="transparent", height=36)
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        head.pack_propagate(False)
        self.d_title = ctk.CTkLabel(head, text="详情", font=ctk.CTkFont(FONT, 15, "bold"),
                                    anchor="w", height=32, width=380)
        self.d_title.pack(side="left")
        # 注：「使用中」不再用独立徽章控件（小控件自动测量是抽搐源），并入标题文字

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=6)
        body.columnconfigure(0, weight=1)

        # 详情标签全部固定像素宽高：换供应商时只换文字，面板永不横向伸缩
        self.d_comment = ctk.CTkLabel(body, anchor="w", justify="left", width=380,
                                      wraplength=370, height=64, font=ctk.CTkFont(FONT, 12),
                                      text_color="#cfcfd6")
        self.d_comment.pack(anchor="w", pady=(4, 8))
        for attr, caption in (("d_url", "BASE_URL"), ("d_model", "实际模型"),
                              ("d_key", "密钥"), ("d_top", "顶层 model")):
            cap = ctk.CTkLabel(body, text=caption, anchor="w", text_color=TEXT_DIM,
                               font=ctk.CTkFont(FONT, 11), height=20)
            cap.pack(anchor="w", pady=(4, 0))
            lbl = ctk.CTkLabel(body, anchor="w", justify="left", width=380,
                               wraplength=370, height=32, font=ctk.CTkFont("Consolas", 12),
                               text_color="#eaeaea")
            lbl.pack(anchor="w")
            setattr(self, attr, lbl)

        btns = ctk.CTkFrame(panel, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 14))
        btns.columnconfigure((0, 1), weight=1)
        self.btn_switch = ctk.CTkButton(btns, text="⚡ 切换到这家", height=40, width=380,
                                        corner_radius=10,
                                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                        font=ctk.CTkFont(FONT, 14, "bold"),
                                        command=self.switch_selected)
        self.btn_switch.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.btn_test = ctk.CTkButton(btns, text="⚡ 测试连接", height=34, width=180, corner_radius=8,
                                      fg_color=CARD_HOVER, hover_color="#3d3d46",
                                      border_width=1, border_color=BORDER,
                                      font=ctk.CTkFont(FONT, 12),
                                      command=self.run_test)
        self.btn_test.grid(row=1, column=0, sticky="ew", padx=(0, 5))
        self.btn_edit = ctk.CTkButton(btns, text="✎ 编辑", height=34, width=180, corner_radius=8,
                                      fg_color=CARD_HOVER, hover_color="#3d3d46",
                                      border_width=1, border_color=BORDER,
                                      font=ctk.CTkFont(FONT, 12),
                                      command=self.edit_provider)
        self.btn_edit.grid(row=1, column=1, sticky="ew", padx=(5, 0))
        self.btn_dup = ctk.CTkButton(btns, text="⧉ 复制", height=34, width=180, corner_radius=8,
                                     fg_color=CARD_HOVER, hover_color="#3d3d46",
                                     border_width=1, border_color=BORDER,
                                     font=ctk.CTkFont(FONT, 12),
                                     command=self.duplicate_provider)
        self.btn_dup.grid(row=2, column=0, sticky="ew", padx=(0, 5), pady=(8, 0))
        self.btn_del = ctk.CTkButton(btns, text="✖ 删除", height=34, width=180, corner_radius=8,
                                     fg_color=CARD_HOVER, hover_color="#4a2f33",
                                     border_width=1, border_color=BORDER,
                                     text_color="#e8a0a6",
                                     font=ctk.CTkFont(FONT, 12),
                                     command=self.delete_provider)
        self.btn_del.grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=(8, 0))
        self._detail_btns = [self.btn_switch, self.btn_test, self.btn_edit, self.btn_dup, self.btn_del]

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(8, 12))
        self.lbl_status = ctk.CTkLabel(bar, text="就绪。切换后需重开 claude 会话生效。",
                                       anchor="w", font=ctk.CTkFont(FONT, 12),
                                       text_color=TEXT_DIM)
        self.lbl_status.pack(side="left", fill="x", expand=True)

    # ----- 数据 -----

    def reload(self):
        self.presets, p_restored, p_corrupt = load_json_or_bak(PRESETS)
        if self.presets is None:
            self.presets = {}
        self.settings, s_restored, s_corrupt = load_json_or_bak(SETTINGS)
        if self.settings is None:
            self.settings = {}
        if p_restored or s_restored:
            messagebox.showwarning(
                "配置已恢复",
                "检测到配置文件损坏，已自动从 .bak 备份恢复。\n"
                f"{'providers.json ' if p_restored else ''}{'settings.json' if s_restored else ''}".strip())
        elif p_corrupt or s_corrupt:
            messagebox.showerror(
                "配置文件损坏",
                "配置文件损坏且没有可用备份，已以空配置启动。\n"
                "损坏的原文件保留为 .corrupt，如有需要可手工抢救。\n"
                f"{'providers.json ' if p_corrupt else ''}{'settings.json' if s_corrupt else ''}".strip())
        env = self.settings.get("env", {})
        self.cur_name = detect_current(self.presets, env)
        if self.selected_name not in self.presets:
            self.selected_name = self.cur_name if self.cur_name in self.presets else None

        for w in self.scroller.winfo_children():
            w.destroy()
        self.cards.clear()
        for name, p in self.presets.items():
            card = ProviderCard(self.scroller, self, name, p, name == self.cur_name)
            if name == self.selected_name:
                card.set_selected(True)
            card.pack(fill="x", padx=4, pady=4)
            self.cards[name] = card
        self._prev_selected = self.selected_name
        self.lbl_count.configure(text=f"供应商预设（{len(self.presets)}）")
        self._update_status_card()
        self._render_detail()

    def _update_status_card(self):
        cur = self.presets.get(self.cur_name, {})
        env = self.settings.get("env", {})
        self.lbl_cur.configure(
            text=f"当前供应商：{self.cur_name}" + (f" — {cur.get('comment', '')}" if cur else ""))
        self.lbl_url.configure(
            text=f"BASE_URL = {env.get('ANTHROPIC_BASE_URL', '(未设置)')}      "
                 f"顶层 model = {self.settings.get('model', '(跟随服务端映射)')}")
        self.lbl_actual.configure(text=f"实际模型 = {cur.get('actual', '（不匹配任何预设）')}")

    def select_provider(self, name):
        # 选中卡以紫色标出：只给「旧选中 + 新选中」两张卡换色，其余卡片零重绘
        for n in dict.fromkeys((self._prev_selected, name)):
            card = self.cards.get(n)
            if card is not None:
                card.set_selected(n == name)
        self._prev_selected = name
        self.selected_name = name
        self._render_detail()

    def _render_detail(self):
        name = self.selected_name
        if not name or name not in self.presets:
            self.d_title.configure(text="详情", text_color="#eaeaea")
            self.d_comment.configure(text="在左侧单击供应商查看详情；\n双击卡片可直接切换。")
            for attr in ("d_url", "d_model", "d_key", "d_top"):
                getattr(self, attr).configure(text="—")
            if self._detail_active:
                for b in self._detail_btns:
                    b.configure(state="disabled")
                self._detail_active = False
            return
        # 按钮状态只在「无选中 ↔ 有选中」切换时变更，避免每次点击都重刷 5 个按钮
        if not self._detail_active:
            for b in self._detail_btns:
                b.configure(state="normal")
            self._detail_active = True
        p = self.presets[name]
        s = p.get("set", {})
        model = (s.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                 or s.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
                 or s.get("ANTHROPIC_DEFAULT_HAIKU_MODEL") or "?")
        # 详情面板展示的正是正在查看的供应商，标题用紫色与左侧选中卡对应
        self.d_title.configure(
            text=f"●  {name}" if name == self.cur_name else name,
            text_color=PURPLE)
        self.d_comment.configure(text=p.get("comment", "") or "（无说明）")
        self.d_url.configure(text=s.get("ANTHROPIC_BASE_URL", "—"))
        self.d_model.configure(text=p.get("actual") or model)
        self.d_key.configure(text=mask(s.get("ANTHROPIC_AUTH_TOKEN", "")))
        self.d_top.configure(text=str(p.get("model") or "（不指定）"))

    def set_status(self, text, color=TEXT_DIM):
        self.lbl_status.configure(text=text, text_color=color)

    def _safe_save_presets(self):
        """落盘预设；失败弹错并返回 False（调用方不应再改动内存状态后继续）。"""
        try:
            save_json(PRESETS, self.presets)
            return True
        except Exception as e:
            messagebox.showerror("保存失败", f"写入 providers.json 失败：\n{e}")
            return False

    # ----- 操作 -----

    def switch_selected(self):
        if self.selected_name:
            self.switch_to(self.selected_name)

    def switch_to(self, name):
        if self._test_running:
            self.set_status("正在实测中，请等测试结束再切换", "#d9b25f")
            return
        p = self.presets.get(name)
        if not p:
            return
        token = p.get("set", {}).get("ANTHROPIC_AUTH_TOKEN", "")
        if PLACEHOLDER_HINT in token:
            if not messagebox.askyesno(
                    "密钥未填",
                    f"[{name}] 的密钥还是占位符，切换后 Claude Code 将无法请求。\n仍要继续吗？"):
                return
        try:
            do_switch(self.presets, self.settings, name)
        except Exception as e:
            messagebox.showerror("切换失败", f"写入 settings.json 失败：\n{e}\n\n配置未变更，可重试。")
            return
        # 原地更新：不重建列表，滚动位置与选中状态保持不动
        old = self.cur_name
        self.cur_name = name
        if old != name:
            if old in self.cards:
                self.cards[old].set_current(False)
            if name in self.cards:
                self.cards[name].set_current(True)
        self._update_status_card()
        self._render_detail()
        url = p.get("set", {}).get("ANTHROPIC_BASE_URL", "?")
        self.set_status(f"✅ 已切换到 [{name}]（{url}）— 重开 claude 会话后生效", GREEN)
        messagebox.showinfo("已切换",
                            f"已切换到 [{name}]\nBASE_URL = {url}\n\n重开 claude 会话后生效。\n随时用命令行 ccs now 交叉确认。")
    def add_provider(self):
        def on_save(name, preset, orig):
            self.presets[name] = preset
            if not self._safe_save_presets():
                del self.presets[name]
                return
            self.selected_name = name
            self.reload()
            self.set_status(f"已新建预设 [{name}]", GREEN)
        ProviderDialog(self, on_save=on_save)

    def edit_provider(self):
        if not self.selected_name:
            return
        self._edit(self.selected_name)

    def _edit(self, name):
        def on_save(new_name, preset, orig):
            if orig and orig != new_name:
                keys = list(self.presets.keys())
                idx = keys.index(orig)
                del self.presets[orig]
                items = list(self.presets.items())
                items.insert(idx, (new_name, preset))
                self.presets = dict(items)
            else:
                self.presets[new_name] = preset
            if not self._safe_save_presets():
                return
            self.selected_name = new_name
            self.reload()
            self.set_status(f"已保存预设 [{new_name}]", GREEN)
        ProviderDialog(self, preset=self.presets.get(name), name=name, on_save=on_save)

    def duplicate_provider(self):
        if not self.selected_name:
            return
        src = self.selected_name
        new_name = src + "-copy"
        i = 2
        while new_name in self.presets:
            new_name = f"{src}-copy{i}"
            i += 1

        def on_save(name, preset, orig):
            self.presets[name] = preset
            if not self._safe_save_presets():
                del self.presets[name]
                return
            self.selected_name = name
            self.reload()
            self.set_status(f"已新建预设 [{name}]", GREEN)
        ProviderDialog(self, preset=_copy.deepcopy(self.presets[src]), name=new_name,
                       on_save=on_save)

    def delete_provider(self):
        name = self.selected_name
        if not name or name not in self.presets:
            return
        msg = f"确定删除预设 [{name}] 吗？"
        if name == self.cur_name:
            msg += "\n\n注意：这是当前正在使用的供应商，删除后 settings.json 里的配置不会变，\n但建议先切换到别家再删。"
        if not messagebox.askyesno("删除确认", msg):
            return
        removed = self.presets.pop(name)
        if not self._safe_save_presets():
            self.presets[name] = removed  # 落盘失败：内存里还原，保持与磁盘一致
            return
        self.selected_name = None
        self.reload()
        self.set_status(f"已删除预设 [{name}]", TEXT_DIM)

    def run_test(self):
        name = self.selected_name
        if not name or self._test_running:
            return
        p = self.presets[name]
        self._test_running = True
        for b in (self.btn_test, self.btn_switch):
            b.configure(state="disabled")
        self.set_status(f"⏳ 正在实测 [{name}] 的 /v1/messages …（约需几秒）", "#d9b25f")

        def work():
            ok, msg = test_provider(p)
            self.after(0, lambda: self._test_done(name, ok, msg))
        threading.Thread(target=work, daemon=True).start()

    def _test_done(self, name, ok, msg):
        self._test_running = False
        for b in (self.btn_test, self.btn_switch):
            b.configure(state="normal")
        if ok:
            self.set_status(f"⚡ [{name}] 实测通过：{msg}", GREEN)
        else:
            self.set_status(f"⚡ [{name}] 实测失败：{msg}", RED)
        messagebox.showinfo("测试通过" if ok else "测试失败", f"[{name}]\n{msg}")


if __name__ == "__main__":
    App().mainloop()
