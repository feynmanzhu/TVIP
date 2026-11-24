# TVIP

央视、卫视频道播放地址合集。地址来自 IPTV 应用引用的四个公开订阅，每天对照源列表做差量更新：同一条流只改主机/IP，新增路径写入，源里消失的路径删除。

## 订阅

- `https://raw.githubusercontent.com/feynmanzhu/TVIP/main/channels.m3u`
- `https://cdn.jsdelivr.net/gh/feynmanzhu/TVIP@main/channels.m3u`

只保留 HTTP/HTTPS 的央视、卫视。港澳台、地方台和国际频道不收录。

## 来源

| 项目 | 地址 |
| --- | --- |
| Guovin/iptv-api | https://github.com/Guovin/iptv-api |
| YueChan/Live | https://github.com/YueChan/Live |
| iptv-org/iptv | https://github.com/iptv-org/iptv |
| Free-TV/IPTV | https://github.com/Free-TV/IPTV |

播放列表会变化，部分地址可能失效。请只播放你有权观看的内容。

## 本地更新

```bash
python scripts/update.py
python scripts/update.py --self-test
```
