"""Import manually reviewed centres; build four-site units from physical CFG distance."""
from __future__ import annotations

from math import hypot


def build_local_units(mapping, centers, minimum_distance_um, *, additional=False, ranks=None):
    centers = [int(e) for e in centers]
    lower, upper = (1, 10) if additional else (5, 6)
    if not lower <= len(centers) <= upper or len(set(centers)) != len(centers):
        raise ValueError(f"请提供{lower}–{upper}个不同的手动候选中心电极ID")
    ranks = list(range(1, len(centers) + 1)) if ranks is None else list(ranks)
    if (len(ranks) != len(centers) or any(isinstance(r, bool) or not isinstance(r, int)
            or r < 1 or (not additional and r > 10) for r in ranks)
            or len(set(ranks)) != len(ranks)):
        raise ValueError("候选编号必须为不同正整数且与中心逐项对应；初次候选rank限1–10")
    positions = {r["electrode"]: r for r in mapping}
    if any(c not in positions for c in centers):
        raise ValueError("候选中心不属于当前CFG，不允许用附近电极自动替换")
    def distance(a, b):
        return hypot(positions[a]["x"] - positions[b]["x"], positions[a]["y"] - positions[b]["y"])
    for i, a in enumerate(centers):
        for b in centers[i + 1:]:
            if distance(a, b) <= minimum_distance_um:
                raise ValueError(f"中心{a}与{b}距离不满足严格>{minimum_distance_um:g}um")
    units, occupied = [], set()
    for rank, center in zip(ranks, centers):
        nearest = sorted((e for e in positions if e != center), key=lambda e: (distance(center, e), e))[:3]
        if len(nearest) != 3:
            raise ValueError("CFG中电极不足，无法形成四电极unit")
        sites = [center, *nearest]
        if occupied.intersection(sites):
            raise ValueError("四电极unit存在重叠；请重新人工选中心，不替换最近邻")
        occupied.update(sites)
        units.append(dict(unit_id=f"candidate_{rank:02d}", rank=rank, center=center, electrodes=sites,
                          distances_um=[distance(center, e) for e in sites]))
    return units
