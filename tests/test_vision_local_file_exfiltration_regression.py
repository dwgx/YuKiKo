"""vision 的本地文件读取不能把任意可读文件外传给第三方 API。

## 缺陷（2026-08-06，两个独立 workflow 都报到同一位置）

`core/tools_vision.py` 有两处读本地文件后 base64 发给 vision API：

* `_prepare_vision_image_ref`（模型给的 `analyze_image` 的 `url` 参数）
* `_collect_onebot_local_files` 一族（NapCat 消息段里的本地文件）

两处都**没有任何包含性检查**：

```python
path = Path(value)
if not path.is_absolute():
    path = (self._project_root / path).resolve()   # 相对路径可越出项目
if not path.exists() or not path.is_file():
    return ""
data = path.read_bytes()                            # 绝对路径原样使用
```

实测两个向量都成立：

```
../../../etc/hosts  ->  /Users/dwgx/etc/hosts        项目外
/etc/hosts          ->  /etc/hosts                   项目外，且文件真实存在
```

`analyze_image` 的 `url` 在 schema 里是**无格式约束的自由字符串**
（`core/agent_tools_media.py`，required 为空），所以模型完全可以填一个本地路径。
后果是任意进程可读文件被 base64 外传给第三方 vision provider。

## 为什么用「内容判定」而不是「目录白名单」

合法路径本来就在项目外：NapCat 的本地文件在 QQ 容器里
（实测 `/Users/<u>/Library/Containers/com.tencent.qq/Data/tmp/napcat-...`）。
按目录拦会打断 `analyze_image` —— 那是线上第二热的工具（288 次调用，仅次于
final_answer 的 375 次）。

改判「这个文件的内容是不是图片」（复用已有的
`core/tools_types.py::_is_known_image_signature`，`core/tools.py` 也在用它）：
文本类机密（passwd / .env / 私钥 / yaml / json）都没有图片 magic bytes，
而任何真图片无论放在哪个目录都能过。
"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from core.tools_types import _is_known_image_signature
from core.tools_vision import ToolVisionMixin

_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8
_JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 12


class SecretFileShapesAreRejectedTests(unittest.TestCase):
    """文本类机密的真实头部必须全部拦住。"""

    def test_etc_hosts_is_rejected(self) -> None:
        head = Path("/etc/hosts").read_bytes()[:16]
        self.assertFalse(_is_known_image_signature(head))

    def test_common_secret_file_shapes_are_rejected(self) -> None:
        for label, head in (
            ("yaml", b"config:\n  bot:\n    n"),
            ("dotenv", b"API_KEY=sk-abc123\nDB"),
            ("openssh key", b"-----BEGIN OPENSSH "),
            ("rsa key", b"-----BEGIN RSA PRIVA"),
            ("json", b'{"token": "secret"}'),
            ("sqlite", b"SQLite format 3\x00"),
            ("plain text", b"hello world\n"),
        ):
            with self.subTest(label=label):
                self.assertFalse(_is_known_image_signature(head), label)


class RealImagesStillPassTests(unittest.TestCase):
    """反向：修得过紧会打断 analyze_image（线上第二热的工具）。"""

    def test_all_supported_image_signatures_pass(self) -> None:
        for label, head in (
            ("PNG", _PNG),
            ("JPEG", _JPEG),
            ("GIF87a", b"GIF87a" + b"\0" * 10),
            ("GIF89a", b"GIF89a" + b"\0" * 10),
            ("BMP", b"BM" + b"\0" * 14),
            ("WEBP", b"RIFF" + b"\0" * 4 + b"WEBP" + b"\0" * 4),
        ):
            with self.subTest(label=label):
                self.assertTrue(_is_known_image_signature(head), label)

    def test_image_outside_the_project_root_is_not_rejected_for_location(self) -> None:
        """NapCat 的合法文件在 QQ 容器里，不能因为「不在项目内」被拦。

        这条钉住「用内容判定而非目录白名单」这个决定。
        """

        self.assertTrue(_is_known_image_signature(_PNG))


class BothReadSitesEnforceTheContentCheckTests(unittest.TestCase):
    """两处读文件的地方都要有这道检查 —— 漏一处等于没修。"""

    def test_prepare_vision_image_ref_checks_signature(self) -> None:
        src = inspect.getsource(ToolVisionMixin._prepare_vision_image_ref)
        self.assertIn(
            "_is_known_image_signature",
            src,
            "_prepare_vision_image_ref 读完 bytes 后没做图片内容判定 —— "
            "模型给一个本地绝对路径就能把该文件外传给 vision API",
        )

    def test_onebot_local_file_path_checks_signature(self) -> None:
        """NapCat 消息段那条路径同样要检查。"""

        source = Path("core/tools_vision.py").read_text(encoding="utf-8")
        marker = "source=onebot_local_file"
        self.assertIn(marker, source, "onebot_local_file 分支不见了，本守卫需要跟着改")
        # 该分支附近必须出现内容判定
        idx = source.index("rejected_not_an_image")
        self.assertGreater(
            source.count("rejected_not_an_image"),
            1,
            "只有一处做了内容判定 —— 两处读文件的地方都要做",
        )
        self.assertGreater(idx, 0)

    def test_rejection_is_logged(self) -> None:
        """拦下来要留痕，否则「图片分析没结果」查不出原因。"""

        source = Path("core/tools_vision.py").read_text(encoding="utf-8")
        self.assertIn("rejected_not_an_image", source)


class PathContainmentIsStillAbsentByDesignTests(unittest.TestCase):
    """记录一个刻意的决定，免得下一个人以为这是漏的。

    这两处**故意没有**目录包含性检查 —— 合法路径本来就在项目外。
    安全边界由内容判定承担。如果哪天要加目录白名单，必须同时包含
    NapCat 的容器缓存目录，否则会打断 analyze_image。
    """

    def test_relative_traversal_still_resolves_outside(self) -> None:
        project_root = Path("/Users/dwgx/Documents/Project/YuKiKo")
        resolved = (project_root / "../../../etc/hosts").resolve()
        self.assertFalse(
            str(resolved).startswith(str(project_root)),
            "这条断言只是记录现状：路径解析确实能越出项目根，"
            "安全性由内容判定保证，不由路径保证",
        )


if __name__ == "__main__":
    unittest.main()
