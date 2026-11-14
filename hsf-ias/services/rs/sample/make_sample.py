#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成样本栅格生成器（docs/08 §2.5 冻结交付物；V-2 确定性可复现）。

64×64 栅格、4 个地块像元区（对应种子 L5306000001~0004 质心邻域）、NDVI 梯度分布
覆盖 §2.3 四级阈值（0.72 旺长 / 0.50 正常 / 0.30 偏弱 / 0.12 长势差）。
生成逻辑唯一实现点为 services/rs/app/sample.py（_bands：red=base+噪声，
nir 由 NDVI 定义反解，random.Random(20261108) 确定性种子）——本脚本复用之并落 JSON，
供 seed_rs.py 经 POST /scenes 登记或离线检查样本分布。

用法（在仓库根或本目录）：
    python services/rs/sample/make_sample.py [输出目录]     # 缺省写 stdout
    python services/rs/sample/make_sample.py out.json       # 写单文件（两景合并 JSON）
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))            # services/rs/sample
_RS = os.path.dirname(_HERE)                                 # services/rs
for _p in (os.path.join(_RS),                               # app 包根
           os.path.join(os.path.dirname(_RS), "common")):   # hsf_common（app.sample 传递依赖）
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.sample import CELL, H, PARCEL_ORDER, W, _bands, ZONES   # noqa: E402

# 两景目标 NDVI（与 app/sample.py seed() 同一组确定性场景：第二景对 L5306000003 构造
# 0.55→0.25——降幅 0.30>0.15 且后景 0.25<0.30，触发 RULE_NDVIFALL 非农化预警）
SCENE_PLANS = [
    ("RS-SAMPLE-20261009", {"L5306000001": 0.72, "L5306000002": 0.50,
                            "L5306000003": 0.55, "L5306000004": 0.12}),
    ("RS-SAMPLE-20261108", {"L5306000001": 0.71, "L5306000002": 0.49,
                            "L5306000003": 0.25, "L5306000004": 0.13}),
]


def make(scene_ndvi: dict[str, float]) -> dict:
    """单景 → /scenes 登记请求体（契约同 docs/08 §2.4 端点 1）。"""
    red, nir, _, _ = _bands(scene_ndvi)
    return {"source": "SAMPLE", "bands": {"red": red, "nir": nir},
            "cellSizeM": CELL, "width": W, "height": H}


def main(argv: list[str]) -> int:
    out = {"meta": {"size": [W, H], "cellSizeM": CELL, "zones": ZONES,
                    "parcelOrder": PARCEL_ORDER,
                    "note": "NDVI 梯度覆盖 §2.3 四级阈值；第二景对 L5306000003 构造非农化预警降幅"},
           "scenes": [{"sceneCode": code, "targetNdvi": ndvi_map, "payload": make(ndvi_map)}
                      for code, ndvi_map in SCENE_PLANS]}
    text = json.dumps(out, ensure_ascii=False)
    if argv and argv[0] not in ("-",):
        path = argv[0]
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"make_sample: 两景样本已写入 {path}（{W}x{H}，cell={CELL}m）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
