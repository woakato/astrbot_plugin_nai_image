# astrbot-plugin-nai-image

基于 [nai.sta1n.cn](https://nai.sta1n.cn) (NovelAI) 的 AstrBot 生图插件。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/image <提示词>` | 根据提示词生成图片 |
| `/image <提示词> --n=4` | 生成 4 张图片 (1-6) |
| `/image <提示词> --style=anime` | 指定风格 |
| `/image <提示词> --size=横图` | 指定比例 |
| `/quota` | 查询 token 剩余配额 |
| `/imgstatus` | 检查生图服务连通性 |

风格：`vertical` / `comicDoujin` / `r18` / `lolita25d` / `anime` / `galgame` / `custom`
尺寸：`竖图` / `横图` / `方图`

## 配置

插件管理面板填写 `image_gen_key`（必填）及其他高级参数。
详细配置项见 `_conf_schema.json`。

## 联动插件：astrbot_plugin_private_companion（我会永远陪着你）

本插件可以作为 [`astrbot_plugin_private_companion`](https://github.com/menglimi/astrbot_plugin_private_companion) （"我会永远陪着你"）的联动后端使用。插件启动后会在 `127.0.0.1:8765` 起一个 **OpenAI Images API 兼容** 的本地代理，让陪伴插件把生图请求转发到这里，由 nai.sta1n.cn 完成实际生图。

### 食用方法

在 `astrbot_plugin_private_companion` 的生图设置里：

1. **生图方式** 选择"在线 API 生图"
2. **API 地址** 填写：`http://127.0.0.1:8765`
3. **API Key** 任意填写一个占位符即可（NAI 插件不校验）
4. **模型名** 任意填写一个占位符即可（NAI 插件不校验）

这样配置后，当陪伴插件发出生图请求时，本插件会**自动监听 8765 端口**，接管请求、调用 nai.sta1n.cn 出图，然后把 base64 图片传回陪伴插件。

> 本插件的 `image_gen_key` 仍然要填写真实的 nai.sta1n.cn token，配额和生图参数也是从本插件的 `_conf_schema.json` 读取。\n
> 生图密钥可在[千寻寄售](https://www.qianxun1688.com/links/D07F549B)链接中获取平均约0.007/张。\n
> 本人与站点和平台没有任何关系，只是作为分享生图平台并不牟利，生图平台也是完全公益。

---

## 🐛 已知问题（求帮忙 debug）

**作者是笨比，找不到哪里有 bug。**

很奇怪的症状：

- ✅ 用命令触发生图（比如 `/陪伴 生成穿搭`）→ **稳定出图**
- ❌ bot **主动** 生图（比如自动根据语境触发生图）→ **有概率发不出来**

调了半天没摸到规律，欢迎大家 fork 帮忙找 bug 改插件。如果有思路可以提 Issue 或 PR。
