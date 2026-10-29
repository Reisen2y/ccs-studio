# -*- coding: utf-8 -*-
"""Claude Code 供应商一键切换工具
用法:
  ccs list            查看所有供应商和当前状态
  ccs now             确认当前在用哪家、实际跑什么模型
  ccs <名称>          切换到指定供应商
  ccs <名称> --force  密钥还是占位符时强制切换
"""
import json, sys, os

HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
PRESETS = os.path.join(HOME, ".claude", "providers.json")
PLACEHOLDER_HINT = "粘贴"


def mask(k):
    return f"{k[:8]}****{k[-4:]}" if k and len(k) > 14 else ("(未填)" if not k else k[:6] + "****")


def main():
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    presets = json.load(open(PRESETS, encoding="utf-8"))
    settings = json.load(open(SETTINGS, encoding="utf-8"))
    cur_env = settings.get("env", {})

    def match_score(p):
        # 配置指纹：预设里与当前 settings 一致的键越多越匹配，避免同 URL 预设混淆
        return sum(1 for k, v in p["set"].items() if cur_env.get(k) == v)

    best = max(presets.items(), key=lambda kv: match_score(kv[1]), default=(None, None))
    cur_name = best[0] if best[0] and match_score(best[1]) > 0 else "(未知)"

    if not args or args[0] == "list":
        for name, p in presets.items():
            cur = "← 当前 ▸" if name == cur_name else "      "
            print(f"{cur} {name}")
            print(f"        说明: {p.get('comment','')}")
            print(f"        实际模型: {p.get('actual','?')}")
            print(f"        密钥: {mask(p['set'].get('ANTHROPIC_AUTH_TOKEN',''))}")
        print("\n切换: ccs <名称>   确认: ccs now   切换后重开 claude 生效")
        return

    if args[0] == "now":
        p = presets.get(cur_name, {})
        print(f"当前供应商: {cur_name}")
        print(f"  BASE_URL  = {cur_env.get('ANTHROPIC_BASE_URL', '(未设置)')}")
        print(f"  名义模型  = {settings.get('model', '(跟随服务端映射)')}")
        print(f"  实际模型  = {p.get('actual', '?')}")
        print("  会话内可用 /status 交叉确认")
        return

    name = args[0]
    if name not in presets:
        print(f"没有叫 {name} 的供应商。可选: {', '.join(presets)}")
        return
    p = presets[name]
    token = p["set"].get("ANTHROPIC_AUTH_TOKEN", "")
    if PLACEHOLDER_HINT in token and not force:
        print(f"⛔ {name} 的密钥还没填。编辑 {PRESETS} 把 ANTHROPIC_AUTH_TOKEN 换成真实 Key，或用 --force 强制。")
        return

    # 托管键集合 = 所有 preset 涉及的键（切换时统一清理再写入，避免上家的残留）
    managed = set()
    for pr in presets.values():
        managed |= set(pr["set"]) | set(pr.get("unset", []))
    managed |= {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}

    env = settings.setdefault("env", {})
    for k in managed:
        env.pop(k, None)
    env.update(p["set"])
    for k in p.get("unset", []):
        env.pop(k, None)

    if "model" in p:
        if p["model"]:
            settings["model"] = p["model"]
        else:
            settings.pop("model", None)

    json.dump(settings, open(SETTINGS, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"✅ 已切换到 [{name}] {p.get('comment','')}")
    print(f"   BASE_URL  = {p['set'].get('ANTHROPIC_BASE_URL')}")
    print(f"   实际模型  = {p.get('actual','?')}")
    print("   重开 claude 会话后生效。随时用 ccs now 确认当前状态。")


if __name__ == "__main__":
    main()
