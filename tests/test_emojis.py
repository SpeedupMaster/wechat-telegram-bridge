import unittest

from bridge.core import WX_EMOJIS, _normalize_wx_text


# All 109 built-in emojis on the current official WeChat 8.0+ picker panel (Simplified Chinese).
OFFICIAL_WECHAT_109 = (
    "微笑", "撇嘴", "色", "发呆", "得意", "流泪", "害羞", "闭嘴", "睡", "大哭",
    "尴尬", "发怒", "调皮", "呲牙", "惊讶", "难过", "囧", "抓狂", "吐", "偷笑",
    "愉快", "白眼", "傲慢", "困", "惊恐", "憨笑", "悠闲", "咒骂", "疑问", "嘘",
    "晕", "衰", "骷髅", "敲打", "再见", "擦汗", "抠鼻", "鼓掌", "坏笑", "右哼哼",
    "鄙视", "委屈", "快哭了", "阴险", "亲亲", "可怜", "笑脸", "生病", "脸红", "破涕为笑",
    "恐惧", "失望", "无语", "嘿哈", "捂脸", "奸笑", "机智", "皱眉", "耶", "吃瓜",
    "加油", "汗", "天啊", "Emm", "社会社会", "旺柴", "好的", "打脸", "哇", "翻白眼",
    "666", "让我看看", "叹气", "苦涩", "裂开", "嘴唇", "爱心", "心碎", "拥抱", "强",
    "弱", "握手", "胜利", "抱拳", "勾引", "拳头", "OK", "合十", "啤酒", "咖啡",
    "蛋糕", "玫瑰", "凋谢", "菜刀", "炸弹", "便便", "月亮", "太阳", "庆祝", "礼物",
    "红包", "發", "福", "烟花", "爆竹", "猪头", "跳跳", "发抖", "转圈",
)


class WeChatEmojiTests(unittest.TestCase):
    def test_all_109_official_panel_emojis_convert(self):
        missing = []
        for code in OFFICIAL_WECHAT_109:
            token = f"[{code}]"
            rendered = _normalize_wx_text(token)
            if rendered == token or not rendered:
                missing.append(code)
        self.assertEqual(missing, [], f"Unconverted official WeChat emojis: {missing}")

    def test_previously_missing_panel_emojis_render_expected_unicode(self):
        cases = [
            ("正在核算新价格[社会社会]", "正在核算新价格🫡"),
            ("这个嘛[Emm][emm]", "这个嘛🤔🤔"),
            ("太秀了[666]", "太秀了🤙"),
            ("[让我看看]怎么回事", "🫣怎么回事"),
            ("恭喜发财[發][发][福]", "恭喜发财💰💰🏮"),
            ("开心[跳跳][转圈]", "开心🥳💃"),
            ("好冷[发抖]", "好冷🫨"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_wx_text(raw), want)

    def test_classic_and_extra_simplified_codes_convert(self):
        cases = [
            ("[酷][冷汗][可爱][饥饿][流汗]", "😎😰😊🤤😓"),
            ("[大兵][奋斗][疯了][折磨][糗大了]", "😌💪🤪😫😳"),
            ("[左哼哼][哈欠][吓][篮球][乒乓][饭]", "😤🥱😱🏀🏓🍚"),
            ("[示爱][闪电][刀][足球][瓢虫][差劲][爱你][NO]", "💋⚡🗡️⚽🐞👎🤟🙅"),
            ("[爱情][飞吻][怄火][磕头][回头][跳绳][投降][挥手]", "💑😘🤬🙇👀🤸🏳️👋"),
            ("[激动][乱舞][街舞][献吻][左太极][右太极]", "🙌💃💃😘☯️☯️"),
            ("[茶][蜡烛][鸡][小狗][加油加油][加油！][加油!]", "🍵🕯️🐥🐶💪💪💪"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_wx_text(raw), want)

    def test_traditional_chinese_codes_convert(self):
        cases = [
            ("[失敬失敬][社會社會]", "🫡🫡"),
            ("[一言難盡][讓我看看]", "🤔🫣"),
            ("[掩面][捂臉][破涕為笑][紅包]", "🤦🤦😂🧧"),
            ("[嘆息][嘆氣][難受][苦澀][崩潰][裂開]", "😮‍💨😮‍💨🥲🥲💔💔"),
            ("[發呆][流淚][尷尬][調皮][驚訝][難過][發抖][轉圈]", "😳😭😅😜😮😔🫨💃"),
            ("[吃西瓜][打臉][機智][歐耶][吼嘿][皺眉]", "🍉🤦🧐✌️🤣😟"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_wx_text(raw), want)

    def test_english_wechat_codes_convert_without_touching_non_emojis(self):
        cases = [
            ("[Smile][Grin][Facepalm][Doge][Respect]", "🙂😁🤦🐶🫡"),
            ("[Awesome][LetMeSee][Sigh][Hurt][Broken][Lol]", "🤙🫣😮‍💨🥲💔😂"),
            ("[Let Down][Bah！R][Bah!R][Bah！L][Bah!L][Worship]", "😞😤😤😤😤🙏"),
            ("[Packet][Rich][Blessing][Waddle][Tremble][Twirl]", "🧧💰🏮🥳🫨💃"),
            # Non-emoji brackets and lowercase words must stay untouched
            ("[Link] [图片] [表情] [ok] [no] [smile] [unknown]", "[Link] [图片] [表情] [ok] [no] [smile] [unknown]"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_wx_text(raw), want)

    def test_legacy_slash_codes_convert_safely(self):
        cases = [
            ("好的/::)明天见", "好的🙂明天见"),
            ("/::)/::D/:rose/:strong/:ok", "🙂😁🌹👍👌"),
            ("/:rose/:rose", "🌹🌹"),
            ("太棒了 /:handclap /:bye!", "太棒了 👏 👋!"),
            ("什么意思/:?", "什么意思❓"),
            ("/:jump/:shake/:circle", "🥳🫨💃"),
            # URLs, API route patterns, identifiers, and Douyin share strings must stay untouched
            ("https://example.com/a/:v/b?x=/:ok", "https://example.com/a/:v/b?x=/:ok"),
            ("http://127.0.0.1:8080/api/:no", "http://127.0.0.1:8080/api/:no"),
            ("GET /users/:no/items", "GET /users/:no/items"),
            ("route /:v/users and /api/:v1 and foo/:ok and /:notify", "route /:v/users and /api/:v1 and foo/:ok and /:notify"),
            ("0.00 09/26 :1pm E@h.OK IiC:/ 文本", "0.00 09/26 :1pm E@h.OK IiC:/ 文本"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_wx_text(raw), want)

    def test_legacy_softbank_pua_codes_convert(self):
        cases = [
            ("你好\ue415\ue056", "你好😄😊"),
            ("哈哈\ue412\ue404\ue022\ue032", "哈哈😂😁❤️🌹"),
            ("\ue04a\ue011\ue252", "☀️✌️⚠️"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_wx_text(raw), want)

    def test_clip_text_does_not_split_multi_codepoint_emoji(self):
        from bridge.core import _clip_text

        self.assertEqual(_clip_text("a" * 199 + "😮‍💨b", 200), "a" * 199)
        self.assertEqual(_clip_text("a" * 198 + "😮‍💨b", 200), "a" * 198)
        self.assertEqual(_clip_text("a" * 199 + "❤️b", 200), "a" * 199)
        self.assertEqual(_clip_text("a" * 198 + "❤️b", 200), "a" * 198 + "❤️")

    def test_wx_emojis_export_keeps_existing_keys_without_duplicates(self):
        from bridge import emojis

        seen_brackets: set[str] = set()
        seen_slashes: set[str] = set()
        for _, brackets, slashes in emojis._EMOJI_SPECS:
            for code in brackets:
                self.assertNotIn(code, seen_brackets, f"Duplicate bracket code: {code}")
                seen_brackets.add(code)
            for slash in slashes:
                self.assertNotIn(slash, seen_slashes, f"Duplicate slash code: {slash}")
                seen_slashes.add(slash)

        for k, v in (("微笑", "🙂"), ("呲牙", "😁"), ("捂脸", "🤦"), ("破涕为笑", "😂"), ("社会社会", "🫡")):
            self.assertEqual(WX_EMOJIS.get(k), v)

    def test_unicode_to_wx_emojis_converts_common_telegram_emojis(self):
        from bridge.emojis import TG_TO_WX_EMOJIS, unicode_to_wx_emojis

        cases = [
            ("好的😁🤦‍♂️🫡", "好的[呲牙][捂脸][社会社会]"),
            ("太离谱了🤦‍♀️🤦🏻‍♂️🤦", "太离谱了[捂脸][捂脸][捂脸]"),
            ("厉害🤙🤙🏻 看看🫣 嗯🤔", "厉害[666][666] 看看[让我看看] 嗯[Emm]"),
            ("加油💪🏻 赞👍🏻 鼓掌👏🏽 胜利✌️✌🏻✌", "加油[加油] 赞[强] 鼓掌[鼓掌] 胜利[胜利][胜利][胜利]"),
            ("感谢🙏🏻 爱你❤️❤ 太阳☀️☀ 害羞☺️☺", "感谢[合十] 爱你[爱心][爱心] 太阳[太阳][太阳] 害羞[害羞][害羞]"),
            ("叹气😮‍💨 惊讶😮 破涕为笑😂 旺柴🐶", "叹气[叹气] 惊讶[惊讶] 破涕为笑[破涕为笑] 旺柴[旺柴]"),
            ("恭喜🎉🎁🧧🎆🧨 发抖🫨", "恭喜[庆祝][礼物][红包][烟花][爆竹] 发抖[发抖]"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(unicode_to_wx_emojis(raw), want)

        # Every reverse mapping target must be a valid current official WeChat 109 panel code
        official_set = set(OFFICIAL_WECHAT_109)
        for src, dst in TG_TO_WX_EMOJIS.items():
            self.assertTrue(dst.startswith("[") and dst.endswith("]"), f"{src} -> {dst}")
            code = dst[1:-1]
            self.assertIn(code, WX_EMOJIS, f"{dst} missing from WX_EMOJIS")
            self.assertIn(code, official_set, f"{dst} not in official 109 WeChat panel")

    def test_unicode_to_wx_emojis_preserves_unrelated_zwj_urls_and_unmapped_emojis(self):
        from bridge.emojis import unicode_to_wx_emojis

        cases = [
            # Multi-codepoint ZWJ emojis whose base is in TG_TO_WX_EMOJIS must NOT be corrupted
            ("燃起来了❤️‍🔥 治愈❤️‍🩹 头晕😵‍💫 摇头🙂‍↔️ 点头🙂‍↕️",
             "燃起来了❤️‍🔥 治愈❤️‍🩹 头晕😵‍💫 摇头🙂‍↔️ 点头🙂‍↕️"),
            # Unmapped emojis & dice emojis stay intact
            ("🎲 4 🎯 🏀 ⚽ 🎳 🎰 🚀", "🎲 4 🎯 🏀 ⚽ 🎳 🎰 🚀"),
            # URLs stay untouched
            ("看 https://example.com/❤️/😁 哈哈😁", "看 https://example.com/❤️/😁 哈哈[呲牙]"),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(unicode_to_wx_emojis(raw), want)


if __name__ == "__main__":
    unittest.main()
