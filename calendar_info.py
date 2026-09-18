"""日本の伝統的な暦注(六曜・一粒万倍日・天赦日)を計算するモジュール。

外部の暦サイトが公開している2026年のカレンダー(六曜の並び、一粒万倍日、
天赦日の実際の日付)と突き合わせて算出式を検証済み。

- 六曜: 旧暦(lunardateで算出)の月+日をもとに算出する。
- 一粒万倍日: 二十四節気の「節」で区切った節月(節切り)ごとに定められた
  十二支の日かどうかで判定する(旧暦の月ではない点に注意)。
- 天赦日: 季節(立春〜立夏/立夏〜立秋/立秋〜立冬/立冬〜立春)ごとに定められた
  特定の干支(60日周期)の日かどうかで判定する。

これらはいずれも科学的根拠のない伝統的な暦注であり、実際の相場変動との
因果関係は確認されていない。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import koyomi
from lunardate import LunarDate

JST = koyomi.JST

ROKUYO_ORDER = ["先勝", "友引", "先負", "仏滅", "大安", "赤口"]
JIKKAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
JUNISHI = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]

# 干支の暦計算アンカー: 2026-07-19 は各種暦サイトで「甲午」の天赦日と確認済み。
_KANSHI_ANCHOR_DATE = date(2026, 7, 19)
_KANSHI_ANCHOR_JIKKAN = 0  # 甲
_KANSHI_ANCHOR_JUNISHI = 6  # 午

# 節月(節切り)ごとの一粒万倍日の十二支(節ごとに0〜11、立春=0起点)。
# 出典: benricho.org の一粒万倍日計算表(実データと突き合わせ検証済み)。
ICHIRYU_MANBAI_TABLE = [
    ("丑", "午"),  # 0: 立春〜啓蟄前日
    ("酉", "寅"),  # 1: 啓蟄〜清明前日
    ("子", "卯"),  # 2: 清明〜立夏前日
    ("卯", "辰"),  # 3: 立夏〜芒種前日
    ("巳", "午"),  # 4: 芒種〜小暑前日
    ("酉", "午"),  # 5: 小暑〜立秋前日
    ("子", "未"),  # 6: 立秋〜白露前日
    ("卯", "申"),  # 7: 白露〜寒露前日
    ("酉", "午"),  # 8: 寒露〜立冬前日
    ("酉", "戌"),  # 9: 立冬〜大雪前日
    ("亥", "子"),  # 10: 大雪〜小寒前日
    ("卯", "子"),  # 11: 小寒〜立春前日
]

# 季節(90度区切り、立春=0起点)ごとの天赦日の干支。
TENSHA_TABLE = ["戊寅", "甲午", "戊申", "甲子"]
SEASON_LABELS = ["春(立春〜立夏前)", "夏(立夏〜立秋前)", "秋(立秋〜立冬前)", "冬(立冬〜立春前)"]


def _sun_longitude(d: date) -> float:
    """dの終わり(翌日0時)時点の太陽黄経を返す。

    節気の境界(30度/90度の切れ目)をまたぐ日は、伝統的に境界後の新しい
    節気に属する日として扱われるため、日中の値ではなく終わり時点の値を
    使うことで、境界日の判定を実際の暦と一致させている。
    """
    dt = datetime(d.year, d.month, d.day, tzinfo=JST) + timedelta(days=1)
    return koyomi.sun_lng(dt)


def get_rokuyo(d: date) -> str:
    ld = LunarDate.from_solar_date(d.year, d.month, d.day)
    index = (ld.month + ld.day - 2) % 6
    return ROKUYO_ORDER[index]


def get_kanshi(d: date) -> str:
    offset = d.toordinal() - _KANSHI_ANCHOR_DATE.toordinal()
    jikkan = JIKKAN[(_KANSHI_ANCHOR_JIKKAN + offset) % 10]
    junishi = JUNISHI[(_KANSHI_ANCHOR_JUNISHI + offset) % 12]
    return jikkan + junishi


def get_season_index(d: date) -> int:
    """立春=0起点で季節を4区分(春夏秋冬)のどれに属するか(0〜3)を返す。"""
    lng = _sun_longitude(d)
    return int(((lng - 315) % 360) // 90)


def get_setsu_index(d: date) -> int:
    """立春=0起点で二十四節気の「節」(12区分、節切り)のどれに属するか(0〜11)を返す。"""
    lng = _sun_longitude(d)
    return int(((lng - 315) % 360) // 30)


def is_ichiryu_manbai(d: date) -> bool:
    setsu = get_setsu_index(d)
    junishi_of_day = get_kanshi(d)[1:]
    return junishi_of_day in ICHIRYU_MANBAI_TABLE[setsu]


def is_tensha(d: date) -> bool:
    season = get_season_index(d)
    return get_kanshi(d) == TENSHA_TABLE[season]


def get_day_info(d: date) -> dict:
    rokuyo = get_rokuyo(d)
    kanshi = get_kanshi(d)
    ichiryu = is_ichiryu_manbai(d)
    tensha = is_tensha(d)
    season = SEASON_LABELS[get_season_index(d)]

    parts = [f"六曜: {rokuyo}", f"干支: {kanshi}", f"季節区分: {season}"]
    parts.append(f"一粒万倍日: {'はい' if ichiryu else 'いいえ'}")
    parts.append(f"天赦日: {'はい' if tensha else 'いいえ'}")

    return {
        "date": d.isoformat(),
        "rokuyo": rokuyo,
        "kanshi": kanshi,
        "season": season,
        "ichiryu_manbai": ichiryu,
        "tensha": tensha,
        "summary": " / ".join(parts),
    }


if __name__ == "__main__":
    import sys

    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today()
    info = get_day_info(target)
    for k, v in info.items():
        print(f"{k}: {v}")
