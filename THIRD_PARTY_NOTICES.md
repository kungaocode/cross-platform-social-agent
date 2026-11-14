# Third-party notices

The project uses external libraries and adapts or references several platform
and video-processing implementations. Their original licenses continue to apply.

| Source | Relationship | Notice |
| --- | --- | --- |
| Tencent `qqbot-nodejs` | Installed SDK; not vendored | Package metadata and upstream distribution license |
| `moxingovo/dsh-bilibili` | Bilibili provider/WBI design reference and adapted code | [Bilibili notice](bilibili/THIRD_PARTY_LICENSE.txt) |
| `dsh-douyin-bridge` contributors | Browser integration reference/adaptation | [Douyin notice](douyin/THIRD_PARTY_LICENSE.txt) |
| `Alterssss/dsh-bili-whale`, `JefferyHcool/BiliNote` | Video evidence-pipeline design references | [Preserved video notice](docs/licenses/video-pipeline.txt) |
| `Userluckytian/dsh-bili-workbench`, `Rimagination/dy-note` | Video workflow research references | Upstream terms apply; these plugins/weights are not bundled |
| `cv-cat/DouYin_Spider` | Optional isolated backend integration | [Installation and distribution boundary](third_party/README.md); source is not bundled |

Python and Node dependencies are installed through their requirement/lock files,
not copied into this repository. wxauto-family packages may have their own
activation, compatibility and use conditions.

No repository-wide open-source license is newly assigned by these notices.
An upstream license is not a blanket license for all code in this project.
