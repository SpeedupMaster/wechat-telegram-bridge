"""WeChat built-in emoji conversion for Telegram.

Covers:
1. All 109 current official WeChat 8.0+ picker emojis plus classic/retired
   built-in bracket codes in Simplified Chinese, Traditional Chinese, and English.
2. Legacy WeChat slash-emoticon codes (``/::)``, ``/:rose``, ``/:ok``, ...),
   matched conservatively outside URLs and identifier/path boundaries.
3. Legacy SoftBank Private Use Area codepoints (``U+E001``–``U+E537``) that
   WeChat clients still render as emojis but Telegram shows as missing-glyph boxes.
"""
from __future__ import annotations

import re

# (unicode_emoji, bracket_codes, legacy_slash_codes)
# Every entry keeps existing bridge.core.WX_EMOJIS mappings intact while adding
# missing official panel emojis, retired built-ins, Traditional Chinese, English,
# and legacy slash codes from WeChat's official emoji tables.
_EMOJI_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # --- Classic 105 WeChat/QQ built-in emoticons (Expression_1 .. Expression_105) ---
    ("🙂", ("微笑", "Smile"), ("/::)",)),
    ("😒", ("撇嘴", "Grimace"), ("/::~",)),
    ("😍", ("色", "Drool"), ("/::B",)),
    ("😳", ("发呆", "發呆", "Scowl"), ("/::|",)),
    ("😎", ("得意", "CoolGuy", "Chill"), ("/:8-)",)),
    ("😭", ("流泪", "流淚", "Sob"), ("/::<",)),
    ("😊", ("害羞", "Shy"), ("/::$",)),
    ("🤐", ("闭嘴", "閉嘴", "Silent", "Shutup"), ("/::X",)),
    ("😴", ("睡", "Sleep"), ("/::Z",)),
    ("😭", ("大哭", "Cry"), ("/::'(",)),
    ("😅", ("尴尬", "尷尬", "Awkward"), ("/::-|",)),
    ("😡", ("发怒", "發怒", "Angry", "Pout"), ("/::@",)),
    ("😜", ("调皮", "調皮", "吐舌", "Tongue", "Wink"), ("/::P",)),
    ("😁", ("呲牙", "Grin"), ("/::D",)),
    ("😮", ("惊讶", "驚訝", "Surprise", "Surprised"), ("/::O",)),
    ("😔", ("难过", "難過", "Frown"), ("/::(",)),
    ("😎", ("酷", "Ruthless", "Cool"), ("/::+",)),
    ("😳", ("囧", "Blush"), ()),
    ("😰", ("冷汗", "Tension"), ("/:--b",)),
    ("😫", ("抓狂", "Scream"), ("/::Q",)),
    ("🤮", ("吐", "Puke"), ("/::T",)),
    ("🤭", ("偷笑", "Chuckle"), ("/:,@P",)),
    ("😊", ("愉快", "可爱", "可愛", "Joyful"), ("/:,@-D",)),
    ("🙄", ("白眼", "Slight"), ("/::d",)),
    ("😤", ("傲慢", "Smug"), ("/:,@o",)),
    ("🤤", ("饥饿", "饑餓", "飢餓", "Hungry"), ("/::g",)),
    ("😪", ("困", "累", "Drowsy"), ("/:|-)",)),
    ("😱", ("惊恐", "驚恐", "Panic"), ("/::!",)),
    ("😓", ("流汗", "Sweat"), ("/::L",)),
    ("😄", ("憨笑", "大笑", "Laugh"), ("/::>",)),
    ("😌", ("悠闲", "悠閑", "悠閒", "大兵", "Commando", "Loafer"), ("/::,@",)),
    ("💪", ("奋斗", "奮鬥", "Determined", "Strive"), ("/:,@f",)),
    ("🤬", ("咒骂", "咒罵", "Scold"), ("/::-S",)),
    ("❓", ("疑问", "疑問", "Shocked", "Doubt"), ("/:?",)),
    ("🤫", ("嘘", "噓", "Shhh"), ("/:,@x",)),
    ("😵", ("晕", "暈", "Dizzy"), ("/:,@@",)),
    ("🤪", ("疯了", "瘋了", "Crazy"), ("/::8",)),
    ("😫", ("折磨", "Tormented"), ()),
    ("😞", ("衰", "Toasted", "BadLuck"), ("/:,@!",)),
    ("💀", ("骷髅", "骷髏", "骷髏頭", "Skull"), ("/:!!!",)),
    ("🔨", ("敲打", "Hammer"), ("/:xx",)),
    ("👋", ("再见", "再見", "Bye", "Wave"), ("/:bye",)),
    ("😅", ("擦汗", "Speechless", "Relief"), ("/:wipe",)),
    ("🤥", ("抠鼻", "摳鼻", "NosePick", "DigNose"), ("/:dig",)),
    ("👏", ("鼓掌", "Clap"), ("/:handclap",)),
    ("😳", ("糗大了", "羞辱", "Shame"), ("/:&-(",)),
    ("😏", ("坏笑", "壞笑", "Trick"), ("/:B-)",)),
    ("😤", ("左哼哼", "Bah！L", "Bah!L"), ("/:<@",)),
    ("😤", ("右哼哼", "Bah！R", "Bah!R"), ("/:@>",)),
    ("🥱", ("哈欠", "Yawn"), ("/::-O",)),
    ("😒", ("鄙视", "鄙視", "Pooh-pooh", "Lookdown"), ("/:>-|",)),
    ("😢", ("委屈", "Shrunken", "Wronged"), ("/:P-(",)),
    ("😢", ("快哭了", "TearingUp", "Puling"), ("/::'|",)),
    ("😈", ("阴险", "陰險", "Sly"), ("/:X-)",)),
    ("😘", ("亲亲", "親親", "Kiss"), ("/::*",)),
    ("😱", ("吓", "嚇", "Wrath", "Uh-oh"), ("/:@x",)),
    ("🥺", ("可怜", "可憐", "Whimper"), ("/:8*",)),
    ("🔪", ("菜刀", "Cleaver"), ("/:pd",)),
    ("🍉", ("西瓜", "Watermelon", "Melon"), ("/:<W>",)),
    ("🍺", ("啤酒", "Beer"), ("/:beer",)),
    ("🏀", ("篮球", "籃球", "Basketball"), ("/:basketb",)),
    ("🏓", ("乒乓", "PingPong"), ("/:oo",)),
    ("☕", ("咖啡", "Coffee"), ("/:coffee",)),
    ("🍚", ("饭", "飯", "Rice"), ("/:eat",)),
    ("🐷", ("猪头", "豬頭", "Pig"), ("/:pig",)),
    ("🌹", ("玫瑰", "Rose"), ("/:rose",)),
    ("🥀", ("凋谢", "凋謝", "枯萎", "Wilt"), ("/:fade",)),
    ("💋", ("嘴唇", "示爱", "示愛", "Lips", "Lip"), ("/:showlove",)),
    ("❤️", ("爱心", "愛心", "Heart"), ("/:heart",)),
    ("💔", ("心碎", "BrokenHeart"), ("/:break",)),
    ("🎂", ("蛋糕", "Cake"), ("/:cake",)),
    ("⚡", ("闪电", "閃電", "Lightning"), ("/:li",)),
    ("💣", ("炸弹", "炸彈", "Bomb"), ("/:bome",)),
    ("🗡️", ("刀", "Dagger"), ("/:kn",)),
    ("⚽", ("足球", "Soccer"), ("/:footb",)),
    ("🐞", ("瓢虫", "瓢蟲", "甲蟲", "Ladybug"), ("/:ladybug",)),
    ("💩", ("便便", "Poop"), ("/:shit",)),
    ("🌙", ("月亮", "Moon"), ("/:moon",)),
    ("☀️", ("太阳", "太陽", "Sun"), ("/:sun",)),
    ("🎁", ("礼物", "禮物", "Gift"), ("/:gift",)),
    ("🤗", ("拥抱", "擁抱", "Hug"), ("/:hug",)),
    ("👍", ("强", "強", "赞", "讚", "ThumbsUp", "Strong"), ("/:strong",)),
    ("👎", ("弱", "ThumbsDown", "Weak"), ("/:weak",)),
    ("🤝", ("握手", "Shake"), ("/:share",)),
    ("✌️", ("胜利", "勝利", "Peace", "Victory"), ("/:v",)),
    ("🙏", ("抱拳", "Salute", "Fight", "Admire"), ("/:@)",)),
    ("🤙", ("勾引", "Beckon"), ("/:jj",)),
    ("✊", ("拳头", "拳頭", "Fist"), ("/:@@",)),
    ("👎", ("差劲", "差勁", "Pinky"), ("/:bad",)),
    ("🤟", ("爱你", "愛你", "RockOn", "Love"), ("/:lvu",)),
    ("🙅", ("NO", "No", "Nuh-uh"), ("/:no",)),
    ("👌", ("OK",), ("/:ok",)),
    ("💑", ("爱情", "愛情", "InLove"), ("/:love",)),
    ("😘", ("飞吻", "飛吻", "Blowkiss"), ("/:<L>",)),
    ("🥳", ("跳跳", "Waddle"), ("/:jump",)),
    ("🫨", ("发抖", "發抖", "Tremble"), ("/:shake",)),
    ("🤬", ("怄火", "慪火", "噴火", "Aaagh!"), ("/:<O>",)),
    ("💃", ("转圈", "轉圈", "Twirl"), ("/:circle",)),
    ("🙇", ("磕头", "磕頭", "Kotow"), ("/:kotow",)),
    ("👀", ("回头", "回頭", "Dramatic", "Lookback"), ("/:turn",)),
    ("🤸", ("跳绳", "跳繩", "JumpRope", "Jump"), ("/:skip",)),
    ("🏳️", ("投降", "Surrender", "Give-in"), ("/:oY",)),
    ("👋", ("挥手", "揮手"), ()),
    ("🙌", ("激动", "激動", "Hooray"), ("/:#-0",)),
    ("💃", ("乱舞", "亂舞", "街舞", "Meditate", "HeyHey"), ("/:hiphot",)),
    ("😘", ("献吻", "獻吻", "Smooch"), ("/:kiss",)),
    ("☯️", ("左太极", "左太極", "TaiChi L", "TaiJi L"), ("/:<&",)),
    ("☯️", ("右太极", "右太極", "TaiChi R", "TaiJi R"), ("/:&>",)),

    # --- Newer WeChat built-in emojis (2_xx & newemoji) ---
    ("😄", ("笑脸", "笑臉", "Happy"), ()),
    ("😷", ("生病", "Sick"), ()),
    ("😳", ("脸红", "臉紅", "Flushed"), ()),
    ("😂", ("破涕为笑", "破涕為笑", "笑哭", "Lol"), ()),
    ("😨", ("恐惧", "恐懼", "Terror"), ()),
    ("😞", ("失望", "Let Down"), ()),
    ("😑", ("无语", "無語", "Duh"), ()),
    ("🤣", ("嘿哈", "吼嘿", "Hey"), ()),
    ("🤦", ("捂脸", "捂臉", "掩面", "Facepalm"), ()),
    ("😏", ("奸笑", "Smirk"), ()),
    ("🧐", ("机智", "機智", "Smart"), ()),
    ("😟", ("皱眉", "皺眉", "Concerned"), ()),
    ("✌️", ("耶", "歐耶", "Yeah!"), ()),
    ("🍉", ("吃瓜", "吃西瓜", "Onlooker"), ()),
    ("💪", ("加油", "GoForIt", "加油加油", "加油！", "加油!", "KeepFighting"), ()),
    ("😓", ("汗", "Sweats"), ()),
    ("😱", ("天啊", "OMG"), ()),
    ("🤔", ("Emm", "emm", "一言難盡", "一言难尽"), ()),
    ("🫡", ("社会社会", "社會社會", "失敬失敬", "Respect"), ()),
    ("🐶", ("旺柴", "Doge"), ()),
    ("👌", ("好的", "NoProb"), ()),
    ("🤦", ("打脸", "打臉", "MyBad"), ()),
    ("🤩", ("哇", "Wow"), ()),
    ("🙄", ("翻白眼", "Boring"), ()),
    ("🤙", ("666", "Awesome"), ()),
    ("🫣", ("让我看看", "讓我看看", "LetMeSee"), ()),
    ("😮‍💨", ("叹气", "嘆氣", "嘆息", "Sigh"), ()),
    ("🥲", ("苦涩", "苦澀", "難受", "Hurt"), ()),
    ("💔", ("裂开", "裂開", "崩潰", "Broken"), ()),
    ("🙏", ("合十", "Worship"), ()),
    ("🎉", ("庆祝", "慶祝", "Party"), ()),
    ("🧧", ("红包", "紅包", "Packet"), ()),
    ("💰", ("發", "发", "Rich"), ()),
    ("🏮", ("福", "Blessing"), ()),
    ("🎆", ("烟花", "煙花", "Fireworks"), ()),
    ("🧨", ("爆竹", "Firecracker"), ()),
    ("🍵", ("茶", "Tea"), ()),
    ("🕯️", ("蜡烛", "蠟燭", "Candle"), ()),
    ("🐥", ("鸡", "雞", "小雞", "Chick"), ()),
    ("🐶", ("小狗", "Pup"), ()),
)

WX_EMOJIS: dict[str, str] = {}
WX_SLASH_EMOJIS: dict[str, str] = {}
for _glyph, _brackets, _slashes in _EMOJI_SPECS:
    for _code in _brackets:
        WX_EMOJIS[_code] = _glyph
    for _slash in _slashes:
        WX_SLASH_EMOJIS[_slash] = _glyph

# Legacy SoftBank Private Use Area emojis supported by WeChat clients, mapped to
# their standard Unicode Emoji equivalents (with emoji variation selectors).
WX_PUA_EMOJIS: dict[str, str] = {
    "\ue001": "👦", "\ue002": "👧", "\ue003": "💋", "\ue004": "👨", "\ue005": "👩", "\ue008": "📷", "\ue009": "☎️", "\ue00c": "💻",
    "\ue00d": "👊", "\ue00e": "👍", "\ue00f": "☝️", "\ue011": "✌️", "\ue014": "⛳", "\ue016": "⚾", "\ue018": "⚽", "\ue01a": "🐴",
    "\ue01d": "✈️", "\ue022": "❤️", "\ue023": "💔", "\ue032": "🌹", "\ue033": "🎄", "\ue034": "💍", "\ue035": "💎", "\ue03c": "🎤",
    "\ue03d": "🎥", "\ue03e": "🎵", "\ue03f": "🔑", "\ue041": "🎸", "\ue044": "🍸", "\ue045": "☕", "\ue047": "🍺", "\ue048": "⛄",
    "\ue049": "☁️", "\ue04a": "☀️", "\ue04b": "☔", "\ue04c": "🌙", "\ue04e": "👼", "\ue04f": "🐱", "\ue050": "🐯", "\ue051": "🐻",
    "\ue052": "🐶", "\ue053": "🐭", "\ue054": "🐳", "\ue055": "🐧", "\ue056": "😊", "\ue057": "😃", "\ue058": "😞", "\ue059": "😠",
    "\ue05a": "💩", "\ue101": "📫", "\ue105": "😜", "\ue106": "😍", "\ue107": "😱", "\ue108": "😓", "\ue109": "🐵", "\ue10a": "🐙",
    "\ue10b": "🐷", "\ue10c": "👽", "\ue10d": "🚀", "\ue10e": "👑", "\ue10f": "💡", "\ue111": "💏", "\ue112": "🎁", "\ue113": "🔫",
    "\ue116": "🔨", "\ue11a": "👿", "\ue11b": "👻", "\ue11c": "💀", "\ue11d": "🔥", "\ue120": "🍔", "\ue126": "💿", "\ue12a": "📺",
    "\ue12b": "👾", "\ue12f": "💰", "\ue131": "🏆", "\ue132": "🏁", "\ue136": "🚲", "\ue138": "🚹", "\ue139": "🚺", "\ue13c": "💤",
    "\ue13d": "⚡", "\ue13f": "🛀", "\ue144": "🔒", "\ue145": "🔓", "\ue147": "🍳", "\ue14c": "💪", "\ue22e": "👆", "\ue22f": "👇",
    "\ue230": "👈", "\ue231": "👉", "\ue24e": "©️", "\ue24f": "®️", "\ue252": "⚠️", "\ue303": "🌺", "\ue307": "🌴", "\ue308": "🌵",
    "\ue30c": "🍻", "\ue30f": "💊", "\ue310": "🎈", "\ue311": "💣", "\ue312": "🎉", "\ue31c": "💄", "\ue322": "👙", "\ue323": "👜",
    "\ue325": "🔔", "\ue329": "💘", "\ue32e": "✨", "\ue331": "💦", "\ue332": "⭕", "\ue333": "❌", "\ue335": "🌟", "\ue336": "❔",
    "\ue337": "❕", "\ue33a": "🍦", "\ue33b": "🍟", "\ue33f": "🍝", "\ue340": "🍜", "\ue344": "🍣", "\ue345": "🍎", "\ue34b": "🎂",
    "\ue401": "😥", "\ue402": "😏", "\ue403": "😔", "\ue404": "😁", "\ue405": "😉", "\ue406": "😣", "\ue407": "😖", "\ue408": "😪",
    "\ue409": "😝", "\ue40a": "😌", "\ue40b": "😨", "\ue40c": "😷", "\ue40d": "😳", "\ue40e": "😒", "\ue40f": "😰", "\ue410": "😲",
    "\ue411": "😭", "\ue412": "😂", "\ue413": "😢", "\ue414": "☺️", "\ue415": "😄", "\ue416": "😡", "\ue417": "😚", "\ue418": "😘",
    "\ue421": "👎", "\ue425": "💑", "\ue42a": "🏀", "\ue42b": "🏈", "\ue435": "🚄", "\ue437": "💝", "\ue43c": "🌂", "\ue43e": "🌊",
    "\ue445": "🎃", "\ue448": "🎅", "\ue520": "🐬", "\ue521": "🐦", "\ue522": "🐠", "\ue524": "🐹", "\ue525": "🐛", "\ue527": "🐨",
    "\ue52a": "🐺", "\ue52b": "🐮", "\ue52c": "🐰", "\ue52d": "🐍", "\ue52e": "🐔", "\ue52f": "🐗", "\ue531": "🐸", "\ue537": "™️",
}

_WX_BRACKET_RE = re.compile(r"\[(" + "|".join(re.escape(k) for k in WX_EMOJIS) + r")\]")
_WX_SLASH_RE = re.compile(
    r"(https?://[^\s<>]+)|("
    + "|".join(re.escape(k) for k in sorted(WX_SLASH_EMOJIS, key=len, reverse=True))
    + r")"
)
_WX_PUA_RE = re.compile(r"[\ue001-\ue537]")
_WX_PUA_TRANS = str.maketrans(WX_PUA_EMOJIS)


def _replace_slash_emojis(text: str) -> str:
    last_replaced_end = -1
    n = len(text)

    def _sub(m: re.Match) -> str:
        nonlocal last_replaced_end
        if m.group(1) is not None:
            return m.group(1)
        code = m.group(2)
        start, end = m.span()
        if start > 0 and start != last_replaced_end:
            prev = text[start - 1]
            if prev.isascii() and (prev.isalnum() or prev in "_/-"):
                return code
        if code[-1].isascii() and code[-1].isalnum() and end < n:
            nxt = text[end]
            if nxt.isascii() and (nxt.isalnum() or nxt == "_" or (nxt == "/" and not text.startswith("/:", end))):
                return code
        last_replaced_end = end
        return WX_SLASH_EMOJIS[code]

    return _WX_SLASH_RE.sub(_sub, text)


def replace_wx_emojis(s: str | None) -> str:
    """Convert WeChat built-in emoji codes ([emoji], legacy /:..., SoftBank PUA) into Unicode emojis."""
    text = s or ""
    if not text:
        return ""
    if _WX_PUA_RE.search(text):
        text = text.translate(_WX_PUA_TRANS)
    if "[" in text:
        text = _WX_BRACKET_RE.sub(lambda m: WX_EMOJIS[m.group(1)], text)
    if "/:" in text:
        text = _replace_slash_emojis(text)
    return text


# Optional reverse mapping: common Telegram Unicode Emojis -> official WeChat 8.0+
# built-in [emoji] codes (strictly targeting the 109 official picker emojis).
TG_TO_WX_EMOJIS: dict[str, str] = {
    "🙂": "[微笑]", "😒": "[撇嘴]", "😍": "[色]", "😎": "[得意]", "😢": "[流泪]",
    "☺️": "[害羞]", "🤐": "[闭嘴]", "😴": "[睡]", "😭": "[大哭]", "😅": "[尴尬]",
    "😡": "[发怒]", "😠": "[发怒]", "😜": "[调皮]", "😝": "[调皮]", "😁": "[呲牙]",
    "😮": "[惊讶]", "😲": "[惊讶]", "😔": "[难过]", "😫": "[抓狂]", "🤮": "[吐]",
    "🤭": "[偷笑]", "😊": "[愉快]", "🙄": "[翻白眼]", "😤": "[傲慢]", "😪": "[困]",
    "😱": "[惊恐]", "😀": "[憨笑]", "😌": "[悠闲]", "🤬": "[咒骂]", "❓": "[疑问]",
    "🤫": "[嘘]", "😵": "[晕]", "💀": "[骷髅]", "🔨": "[敲打]", "👋": "[再见]",
    "👏": "[鼓掌]", "😏": "[奸笑]", "😈": "[阴险]", "😘": "[亲亲]", "🥺": "[可怜]",
    "😄": "[笑脸]", "😃": "[笑脸]", "😷": "[生病]", "😳": "[脸红]", "😂": "[破涕为笑]",
    "😨": "[恐惧]", "😞": "[失望]", "😑": "[无语]", "🤣": "[嘿哈]", "🤦": "[捂脸]",
    "🧐": "[机智]", "😟": "[皱眉]", "🍉": "[吃瓜]", "💪": "[加油]", "😓": "[汗]",
    "🤔": "[Emm]", "🫡": "[社会社会]", "🐶": "[旺柴]", "🤩": "[哇]", "🤙": "[666]",
    "🫣": "[让我看看]", "😮‍💨": "[叹气]", "🥲": "[苦涩]", "💋": "[嘴唇]", "❤️": "[爱心]",
    "💔": "[心碎]", "🤗": "[拥抱]", "👍": "[强]", "👎": "[弱]", "🤝": "[握手]",
    "✌️": "[胜利]", "✊": "[拳头]", "👌": "[OK]", "🙏": "[合十]", "🍺": "[啤酒]",
    "☕": "[咖啡]", "🎂": "[蛋糕]", "🌹": "[玫瑰]", "🥀": "[凋谢]", "🔪": "[菜刀]",
    "💣": "[炸弹]", "💩": "[便便]", "🌙": "[月亮]", "☀️": "[太阳]", "🎉": "[庆祝]",
    "🎁": "[礼物]", "🧧": "[红包]", "🎆": "[烟花]", "🧨": "[爆竹]", "🐷": "[猪头]",
    "🫨": "[发抖]",
}

_SKIN_TONE_BASES = {"👋", "👏", "🤦", "💪", "🤙", "👍", "👎", "🤝", "✌", "✊", "👌", "🙏"}
_GENDERED_BASES = {"🤦"}
_TG_TO_WX_BY_BASE: dict[str, str] = {}
_TG_TO_WX_PARTS: list[str] = []
for _emo, _wx_code in sorted(TG_TO_WX_EMOJIS.items(), key=lambda kv: len(kv[0]), reverse=True):
    _base = _emo.rstrip("\ufe0f")
    _TG_TO_WX_BY_BASE[_base] = _wx_code
    _pat = re.escape(_base)
    if _base in _GENDERED_BASES:
        _pat += r"(?:\ufe0f|[\U0001f3fb-\U0001f3ff])?(?:\u200d[\u2640\u2642]\ufe0f?)?"
    elif _base in _SKIN_TONE_BASES:
        _pat += r"(?:\ufe0f|[\U0001f3fb-\U0001f3ff])?"
    else:
        _pat += r"\ufe0f?"
    _TG_TO_WX_PARTS.append(_pat)

_TG_TO_WX_RE = re.compile(
    r"(https?://[^\s<>]+)|(?<!\u200d)("
    + "|".join(_TG_TO_WX_PARTS)
    + r")(?![\ufe0f\u200d\U0001f3fb-\U0001f3ff\u20e3])"
)
_TG_MODIFIER_STRIP_RE = re.compile(r"(?:\ufe0f|[\U0001f3fb-\U0001f3ff])?(?:\u200d[\u2640\u2642]\ufe0f?)?$")


def unicode_to_wx_emojis(s: str | None) -> str:
    """Convert common Telegram Unicode Emojis into WeChat [emoji] codes while leaving URLs & unrelated ZWJ intact."""
    text = s or ""
    if not text or text.isascii():
        return text

    def _sub(m: re.Match) -> str:
        if m.group(1) is not None:
            return m.group(1)
        token = m.group(2)
        if token in _TG_TO_WX_BY_BASE:
            return _TG_TO_WX_BY_BASE[token]
        base = _TG_MODIFIER_STRIP_RE.sub("", token)
        return _TG_TO_WX_BY_BASE.get(base, token)

    return _TG_TO_WX_RE.sub(_sub, text)
