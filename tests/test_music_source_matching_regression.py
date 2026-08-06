"""MusicSourceMatcher 的候选匹配：括号后缀不该把正片挤掉，翻唱不该冒名顶替。

## 背景（2026-08-06 实测，线上 `music_play` 失败链）

`_normalize_text` 的本意写在它自己的注释里 ——「移除括号内容（如 (DJ版)、(伴奏) 等）」，
实现是两条 `re.sub(r'\\([^)]*\\)', ...)`。但它先调 `normalize_matching_text`，
而后者把**所有标点（含括号）替换成空格**，所以那两条正则永远匹配不到任何东西，
是死代码。后果是括号内容被**粘进歌名**参与相似度计算：

    '晴天 (KTV版伴奏)' -> '晴天ktv版伴奏'   name_score = 2/7*0.95 = 0.271
                                            总分 0.49 < 0.5 阈值 -> 拒
    '晴天 (Live)'      -> '晴天live'        总分 0.52，勉强过线

实测代价：kuwo 每次返回的 5 条结果里常常全是伴奏/片段/DJ 版/串烧变体，
带括号后缀的正片因为分数被稀释而拿不到 0.5，于是整个源报 `source_no_result`。
线上那次 `music_play` 四个源全灭，kuwo 就是这样灭的。

## 为什么修括号必须同时加 artist 门

剥掉括号后 `孤勇者 (cover: 陈奕迅)`（演唱者其实是别人）会变成 `孤勇者`，
name_score 拿满 1.0，总分 0.7+0+0.1 = 0.8 **通过** —— 翻唱冒名顶替原唱。
在修之前，它是靠「括号粘进歌名压低分数」这个 bug **偶然**挡住的。

`music.py` 的 `artist_guard` 挡不到这里：那一层作用在 netease 的
`MusicSearchResult` 上，而 `MusicSourceMatcher` 是拿着已确认的歌名+歌手
去别的平台找同一首歌，是另一层。

所以本文件同时钉两个方向：带后缀的正片要能匹配，换了歌手的翻唱要被拒。

数据全部是 2026-08-06 从 `search.kuwo.cn/r.s` 真实抓下来的行（含未解码的
`&nbsp;` 实体和 `\\\\u0026` 形态），不是手编的理想输入。
"""

from __future__ import annotations

import unittest

from core.music_sources import MusicSourceMatcher


def _kuwo_row(name: str, artist: str, duration: str = "0") -> dict[str, str]:
    return {"NAME": name, "SONGNAME": name, "ARTIST": artist, "DURATION": duration}


class NormalizeStripsBracketedQualifiersTests(unittest.TestCase):
    """`_normalize_text` 必须真的去掉括号内容 —— 那两条正则不能是死代码。"""

    def setUp(self) -> None:
        self.matcher = MusicSourceMatcher(timeout=8)

    def test_halfwidth_bracket_content_is_removed(self) -> None:
        self.assertEqual(self.matcher._normalize_text("晴天 (Live)"), "晴天")

    def test_fullwidth_bracket_content_is_removed(self) -> None:
        self.assertEqual(self.matcher._normalize_text("孤勇者（电影主题曲）"), "孤勇者")

    def test_html_entity_bracket_content_is_removed(self) -> None:
        """kuwo 返回未解码的 &nbsp;，剥括号必须在 html.unescape 之后生效。"""

        self.assertEqual(self.matcher._normalize_text("晴天&nbsp;(KTV版伴奏)"), "晴天")

    def test_english_bracket_qualifier_is_removed(self) -> None:
        self.assertEqual(self.matcher._normalize_text("Shape of You (Acoustic)"), "shapeofyou")

    def test_bare_title_is_untouched(self) -> None:
        self.assertEqual(self.matcher._normalize_text("晴天"), "晴天")

    def test_title_that_is_only_a_bracket_group_does_not_become_empty(self) -> None:
        """整个名字都在括号里时不能剥成空串 —— 那会让候选静默消失。"""

        self.assertNotEqual(self.matcher._normalize_text("(instrumental)"), "")


class BracketedOriginalStillMatchesTests(unittest.TestCase):
    """带括号后缀的正片必须能被选中。"""

    def setUp(self) -> None:
        self.matcher = MusicSourceMatcher(timeout=8)

    def test_live_suffix_original_is_matched(self) -> None:
        rows = [_kuwo_row("晴天&nbsp;(Live)", "周杰伦", "180")]
        best = self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo")
        self.assertIsNotNone(
            best,
            "带 (Live) 后缀的正片被拒了 —— 括号内容又被粘进歌名稀释分数了",
        )

    def test_theme_song_suffix_original_is_matched(self) -> None:
        rows = [_kuwo_row("孤勇者&nbsp;(电影主题曲)", "陈奕迅", "256")]
        best = self.matcher._find_best_match(rows, "孤勇者", "陈奕迅", 0, "kuwo")
        self.assertIsNotNone(best, "带 (电影主题曲) 后缀的正片被拒了")

    def test_clean_original_wins_over_bracketed_sibling(self) -> None:
        """正片和变体同时在候选里时，必须挑正片。"""

        rows = [
            _kuwo_row("晴天&nbsp;(Live)", "周杰伦", "200"),
            _kuwo_row("晴天", "周杰伦", "165"),
        ]
        best = self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo")
        self.assertIsNotNone(best)
        self.assertEqual(best["NAME"], "晴天")


class AccompanimentAndCoverAreRejectedTests(unittest.TestCase):
    """剥括号不能顺带放行伴奏和翻唱。"""

    def setUp(self) -> None:
        self.matcher = MusicSourceMatcher(timeout=8)

    def test_accompaniment_is_still_rejected_after_bracket_strip(self) -> None:
        """`(伴奏)` 剥掉后歌名会变成精确匹配 —— 必须靠 marker 守卫拦住。"""

        rows = [_kuwo_row("晴天&nbsp;(伴奏)", "周杰伦", "269")]
        best = self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo")
        self.assertIsNone(best, "伴奏版被当成正片选中了")

    def test_ktv_accompaniment_is_still_rejected(self) -> None:
        rows = [_kuwo_row("晴天&nbsp;(KTV版伴奏)", "周杰伦", "269")]
        self.assertIsNone(self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo"))

    def test_cover_by_other_artist_is_rejected(self) -> None:
        """线上真实行：`孤勇者 (cover: 陈奕迅)` 的演唱者是别人。

        剥掉括号后歌名满分，只有 artist 门能挡住它。
        """

        rows = [_kuwo_row("孤勇者&nbsp;(cover:&nbsp;陈奕迅)", "福妖", "256")]
        best = self.matcher._find_best_match(rows, "孤勇者", "陈奕迅", 0, "kuwo")
        self.assertIsNone(
            best,
            "翻唱冒名顶替原唱 —— 请求的是陈奕迅，候选歌手是福妖",
        )

    def test_cover_of_a_different_original_artist_is_rejected(self) -> None:
        rows = [_kuwo_row("Life&apos;s&nbsp;A&nbsp;Struggle&nbsp;(cover:&nbsp;宋岳庭)", "黄祥柱", "128")]
        best = self.matcher._find_best_match(rows, "Life's A Struggle", "宋岳庭", 0, "kuwo")
        self.assertIsNone(best, "翻唱冒名顶替原唱 —— 请求的是宋岳庭，候选歌手是黄祥柱")

    def test_wrong_song_by_right_artist_is_rejected(self) -> None:
        """歌手对但歌名完全不同 —— kuwo 常见的「同歌手其它歌」噪音。"""

        rows = [_kuwo_row("花海", "周杰伦", "210")]
        self.assertIsNone(self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo"))

    def test_medley_is_rejected(self) -> None:
        """演唱会串烧里含目标歌名，但不是那首歌。"""

        rows = [
            _kuwo_row(
                "志明与春娇+听妈妈的话+干杯+晴天+离开地球表面+双截棍&nbsp;(2013小巨蛋演唱会第三场)",
                "周杰伦\\\\u0026五月天",
                "787",
            )
        ]
        self.assertIsNone(self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo"))


class RealKuwoWindowIsRejectedForTheRightReasonsTests(unittest.TestCase):
    """整窗真实数据：2026-08-06 搜「晴天 周杰伦」拿到的 20 条（rn=20）。

    这一窗里**没有**周杰伦《晴天》的正片 —— 只有 2 条伴奏、7 条他人翻唱、
    以及同歌手的其它歌。所以正确答案就是 None。

    本例的价值在于「拒绝的理由要对」：翻唱把原唱名字塞进**标题**
    （`晴天（周杰伦）` 实际演唱者是青崖）来骗搜索，剥掉括号后歌名拿满分，
    总分 0.7+0+0.1 = 0.8 会过阈值 —— 只有 artist 门能挡住。
    """

    WINDOW = [
        ("晴天&nbsp;(KTV版伴奏)", "周杰伦"),
        ("志明与春娇+听妈妈的话+干杯+晴天+离开地球表面+双截棍&nbsp;(2013小巨蛋演唱会第三场)", "周杰伦\\\\u0026五月天"),
        ("晴天&nbsp;(伴奏)", "周杰伦"),
        ("新歌前奏&nbsp;+&nbsp;Julia&nbsp;+&nbsp;晴天&nbsp;(片段)", "周杰伦\\\\u0026인피니트"),
        ("花海&nbsp;(DJ&nbsp;阿若版)", "周杰伦"),
        ("听妈妈的话&nbsp;(完整版|DJ彭鹏版)", "周杰伦"),
        ("雨下一整晚&nbsp;(纯音乐)", "周杰伦"),
        ("晴天&nbsp;(原唱:周杰伦)&nbsp;激情吃力版", "冬菇滑稽"),
        ("晴天（吉他弹唱）Cover&nbsp;周杰伦&nbsp;Jay", "暂停白昼"),
        ("晴天（周杰伦）", "青崖"),
        ("蜗牛", "周杰伦"),
        ("烟花易冷&nbsp;(片段)", "周杰伦"),
        ("夜曲&nbsp;(mp3.2)", "周杰伦"),
        ("晴天---（周杰伦）", "十二月"),
        ("晴天", "金布袋"),
        ("晴天&nbsp;(cover:&nbsp;凌霄(庄云霄))&nbsp;(Live)", "木辛minimo"),
        ("淘汰&nbsp;(Demo版)", "周杰伦"),
        ("淘汰&nbsp;(2007上海演唱会)", "周杰伦"),
        ("花海&nbsp;(片段)", "周杰伦"),
        ("晴天&nbsp;(cover:&nbsp;娄艺潇)", "藤井瑶"),
    ]

    def setUp(self) -> None:
        self.matcher = MusicSourceMatcher(timeout=8)
        self.rows = [_kuwo_row(name, artist) for name, artist in self.WINDOW]

    def test_no_candidate_is_served_when_the_window_has_no_original(self) -> None:
        best = self.matcher._find_best_match(self.rows, "晴天", "周杰伦", 0, "kuwo")
        self.assertIsNone(
            best,
            "这一窗里没有周杰伦《晴天》正片，任何被选中的候选都是错的",
        )

    def test_cover_that_puts_original_artist_in_the_title_is_rejected(self) -> None:
        """`晴天（周杰伦）` 演唱者是青崖 —— 标题里的歌手名不算歌手。"""

        rows = [_kuwo_row("晴天（周杰伦）", "青崖")]
        self.assertIsNone(self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo"))

    def test_same_title_by_unrelated_artist_is_rejected(self) -> None:
        """`晴天` by 金布袋 —— 同名不同曲，歌名满分但歌手不符。"""

        rows = [_kuwo_row("晴天", "金布袋")]
        self.assertIsNone(self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo"))

    def test_original_is_picked_when_it_is_present_in_the_same_window(self) -> None:
        """同一窗里插入正片后必须选中它，证明上面的 None 不是「一律拒绝」。"""

        rows = [*self.rows, _kuwo_row("晴天", "周杰伦", "165")]
        best = self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo")
        self.assertIsNotNone(best, "窗里有正片却没选中 —— 门开得太紧了")
        self.assertEqual(best["ARTIST"], "周杰伦")
        self.assertEqual(best["NAME"], "晴天")


class ArtistGateToleratesLegitimateVariationTests(unittest.TestCase):
    """artist 门不能把合法的歌手写法差异也一起拒了。"""

    def setUp(self) -> None:
        self.matcher = MusicSourceMatcher(timeout=8)

    def test_featuring_artist_string_still_matches(self) -> None:
        """kuwo 把合作歌手拼成 `周杰伦\\\\u0026五月天`，仍应视为同一歌手。"""

        rows = [_kuwo_row("说好不哭", "周杰伦\\\\u0026五月天", "265")]
        best = self.matcher._find_best_match(rows, "说好不哭", "周杰伦", 0, "kuwo")
        self.assertIsNotNone(best, "合作/feat 形态的歌手串被 artist 门误拒了")

    def test_unknown_target_artist_does_not_block_matching(self) -> None:
        """调用方没给歌手时，artist 门必须完全不参与判定。"""

        rows = [_kuwo_row("晴天", "周杰伦", "165")]
        best = self.matcher._find_best_match(rows, "晴天", "", 0, "kuwo")
        self.assertIsNotNone(best, "目标歌手为空时 artist 门不该生效")

    def test_missing_candidate_artist_does_not_block_matching(self) -> None:
        """候选缺歌手字段时不该被当成「歌手不符」。"""

        rows = [_kuwo_row("晴天", "", "165")]
        best = self.matcher._find_best_match(rows, "晴天", "周杰伦", 0, "kuwo")
        self.assertIsNotNone(best, "候选歌手字段为空时不该判定为不匹配")


class QueryKeywordKeepsWordBoundariesTests(unittest.TestCase):
    """发给上游的搜索关键词不能用「匹配用」的激进形态。

    `_normalize_text` 是为**比较**设计的：删空格、删撇号、全小写。
    `find_alternative` 却拿它的输出直接当搜索词，于是：

        "Life's a Struggle" -> "lifesastruggle"

    实测线上日志就是 `searching_source | source=kuwo | song=lifesastruggle`，
    等于拿一个不存在的词去搜 —— 英文歌名基本必然 `source_no_result`。
    """

    def setUp(self) -> None:
        self.matcher = MusicSourceMatcher(timeout=8)

    def test_apostrophe_title_keeps_word_spacing(self) -> None:
        keyword = self.matcher._normalize_query_text("Life's a Struggle")
        self.assertIn(" ", keyword, f"英文歌名被压成了单词团: {keyword!r}")
        self.assertNotEqual(keyword.replace(" ", ""), keyword)

    def test_query_form_preserves_all_words(self) -> None:
        keyword = self.matcher._normalize_query_text("Shape of You")
        self.assertEqual(keyword.lower().split(), ["shape", "of", "you"])

    def test_chinese_title_is_unchanged(self) -> None:
        self.assertEqual(self.matcher._normalize_query_text("晴天"), "晴天")

    def test_html_entities_are_decoded_for_query(self) -> None:
        self.assertEqual(self.matcher._normalize_query_text("晴天&nbsp;Live").split(), ["晴天", "Live"])

    def test_find_alternative_sends_spaced_keyword_to_sources(self) -> None:
        """端到端：find_alternative 传给 _search_source 的歌名必须保留词边界。"""

        import asyncio

        seen: list[tuple[str, str]] = []

        async def fake_search_source(source, song_name, artist, duration_ms):
            seen.append((song_name, artist))
            return None

        self.matcher._search_source = fake_search_source  # type: ignore[method-assign]
        asyncio.run(self.matcher.find_alternative("Life's a Struggle", "宋岳庭", 0, ["kuwo"]))

        self.assertTrue(seen, "_search_source 没被调用，测试失去意义")
        song_sent = seen[0][0]
        self.assertIn(
            " ",
            song_sent,
            f"传给音源的歌名丢了词边界: {song_sent!r} —— 上游搜不到这种词",
        )


class MatcherRequestShapeTests(unittest.TestCase):
    """请求构造层面的实测事实，别再让下一个人重新抓一遍包。"""

    def test_kuwo_result_window_is_wide_enough_for_variant_noise(self) -> None:
        """rn=5 会被伴奏/片段/DJ 版/串烧占满，正片被挤出窗口。

        2026-08-06 实测：搜「晴天 周杰伦」，某次返回的 5 条全是变体，
        另一次正片排在第 3 —— 窗口太窄时命中与否纯看运气。
        """

        import inspect

        src = inspect.getsource(MusicSourceMatcher._search_kuwo)
        self.assertNotIn(
            '"rn": 5',
            src,
            "kuwo 搜索窗口还是 5 条 —— 变体噪音会把正片挤出去",
        )

    def test_migu_search_uses_the_parameter_name_the_api_requires(self) -> None:
        """migu 主端点已把参数名从 keyword 改成 text。

        2026-08-06 实测：带 keyword= 返回
        `code=299999 info='参数校验失败 text:不能为空'`（HTTP 200，所以不抛异常，
        只是 0 条结果），代码于是回退到 legacy 端点，那个端点 301 到一个 HTML SPA。
        日志里看到的 `migu 301` 是这个二段失败的尾巴，不是根因。
        """

        import inspect

        src = inspect.getsource(MusicSourceMatcher._search_migu)
        self.assertIn('"text"', src, "migu 搜索还在用 keyword= —— 上游要求 text=")
        self.assertIn(
            "searchSwitch",
            src,
            "migu 只带 text= 会返回 code=000000 但 0 条结果，必须带 searchSwitch 才有歌曲结果",
        )


if __name__ == "__main__":
    unittest.main()
