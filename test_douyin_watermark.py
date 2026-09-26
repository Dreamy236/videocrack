# -*- coding: utf-8 -*-
"""临时验证脚本：确认抖音直链优先级已改为「无水印源优先」。

模拟新版抖音 API 的 video 节点：
- download_addr 带 watermark=1（新版已带水印，旧逻辑会误选它）
- play_addr_h264 / bit_rate[].play_addr 为 api-play-hl 无水印源
- play_addr 为 playwm（带水印）
期望：选中 play_addr_h264（无水印）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collector_core import BrowserSearcher

WM = "https://api-play.amemv.com/aweme/v1/play/?video_id=xxx&watermark=1&media_type=4"
H264 = "https://api-play-hl.amemv.com/aweme/v1/play/?video_id=xxx&file_id=h264src"
BR_HI = "https://api-play-hl.amemv.com/aweme/v1/play/?video_id=xxx&file_id=bitrate_high"
BR_LO = "https://api-play-hl.amemv.com/aweme/v1/play/?video_id=xxx&file_id=bitrate_low"
PLAYWM = "https://api-play.amemv.com/aweme/v1/playwm/?video_id=xxx"


def build(video):
    info = {"aweme_id": "123456", "desc": "测试视频", "video": video,
            "statistics": {}, "author": {"nickname": "测试作者"}}
    return BrowserSearcher._build_douyin(info, {"host": "https://www.douyin.com/video/"}, "")


def check(name, got, expect):
    ok = got == expect
    print(("[PASS] " if ok else "[FAIL] ") + name)
    print("       实际: " + str(got))
    if not ok:
        print("       期望: " + str(expect))
    return ok


results = []

# 场景1：完整字段（h264 无水印源存在）→ 应选 h264
v = build({
    "download_addr": {"url_list": [WM]},
    "play_addr_h264": {"url_list": [H264]},
    "play_addr": {"url_list": [PLAYWM]},
    "bit_rate": [
        {"bit_rate": 520177, "play_addr": {"url_list": [BR_LO]}},
        {"bit_rate": 981815, "play_addr": {"url_list": [BR_HI]}},
    ],
})
results.append(check("场景1 完整字段 → 取 play_addr_h264（无水印）", v.media_url, H264))

# 场景2：无 h264/bytevc1，仅 bit_rate + download_addr → 取 bit_rate 最高码率
v = build({
    "download_addr": {"url_list": [WM]},
    "play_addr": {"url_list": [PLAYWM]},
    "bit_rate": [
        {"bit_rate": 520177, "play_addr": {"url_list": [BR_LO]}},
        {"bit_rate": 981815, "play_addr": {"url_list": [BR_HI]}},
    ],
})
results.append(check("场景2 仅bit_rate → 取最高码率(981815)无水印源", v.media_url, BR_HI))

# 场景3：只有带水印的 download_addr → 兜底并把 watermark=1 改写为 0
v = build({
    "download_addr": {"url_list": [WM]},
    "play_addr": {"url_list": [PLAYWM]},
})
results.append(check("场景3 仅带水印源 → 兜底改写 watermark=0",
                     v.media_url, WM.replace("watermark=1", "watermark=0")))

# 场景4：图文内容（play_addr 仅为 BGM ies-music）→ 应被过滤，不产生直链
v = build({
    "play_addr": {"url_list": ["https://sf-tb-sg.ibytedtos.com/obj/ies-music/xxx.mp3"]},
})
results.append(check("场景4 图文BGM(ies-music) → 过滤，直链为空", v.media_url, ""))

# 场景5：只有 playwm 路径（无 watermark=1 参数）→ 兜底改写为 /play/
v = build({"play_addr": {"url_list": [PLAYWM]}})
results.append(check("场景5 仅playwm → 识别为水印并改写为 /play/",
                     v.media_url, PLAYWM.replace("/playwm/", "/play/")))

print("\n结果: %d/%d 通过" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
