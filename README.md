# astrbot_plugin_jm_bot

## 所有内容均由gpt-5.4生成不保证功能的可用性请自行参考

适配 AstrBot 的 JM 漫画搜索、选章下载、单图提取与 PDF 发送插件。

> 当前版本：`v0.1.1`

## 功能概览

- `搜jm [关键词] [页码]`
  - 搜索 JM 漫画
  - 支持封面缩略图拼图预览
  - 支持结果缓存与翻页提示
  - 支持补全前若干条作品总页数
- `看jm [序号或id]`
  - 查看作品详情
  - 单章节作品可直接下载
  - 多章节作品支持选章后下载
- `看jm [序号或id] [章节编号/范围]`
  - 支持单章、多章、范围下载，例如 `看jm 1 1,3,5-7`
- `看jm [序号或id] [章节] P[页码]`
  - 提取指定章节中的单张图片
- `随机jm [关键词]`
  - 随机返回符合关键词的作品
- `jm更新域名`
  - 更新可用 HTML 域名
- `jm清空域名`
  - 清空持久化域名配置并恢复 API 模式

## 特性

- 适配 AstrBot 命令事件模型
- 命令执行时阻断 LLM 误触发
- 搜索缓存按会话隔离，避免群聊串号
- 章节选择缓存支持 TTL
- 页数补全缓存支持 TTL 与容量上限
- 搜索封面缓存支持最大文件数淘汰
- PDF 生成走后台线程，降低主事件线程阻塞风险
- `jm_option.yml` 改为稳定持久化结构，避免多线程覆盖与临时文件泄漏
- 文本消息与转发消息支持自动撤回配置
- 域名更新与清空支持持久保留

## 安装

### 方式一：仓库安装

在 AstrBot WebUI 插件市场中使用仓库地址安装：

```text
https://github.com/Huac233/astrbot_plugin_jm_bot
```

### 方式二：本地目录安装

将项目放入 AstrBot 插件目录：

```text
/AstrBot/data/plugins/astrbot_plugin_jm_bot
```

## 依赖

请以 `requirements.txt` 为准：

- `jmcomic>=2.5.29`
- `pillow>=10.0.0`
- `pikepdf>=9.0.0`
- `PyYAML>=6.0.1`
- `aiofiles>=23.2.1`
- `curl-cffi>=0.6.0`

> `aiohttp` 由 AstrBot 运行环境提供，本插件未单独声明。

## 指令说明

### 搜索

```text
搜jm 萝莉
搜jm 韩漫 2
搜jm 萝莉 +无修正 -AI
```

### 查看与下载

```text
看jm 1
看jm 1356854
看jm 1 2
看jm 1 1,3,5-7
```

### 单图提取

```text
看jm 8 2 P5
```

### 随机

```text
随机jm
随机jm 萝莉
```

### 域名管理

```text
jm更新域名
jm清空域名
```

## 配置说明

推荐优先通过 AstrBot WebUI 插件配置面板修改配置。
插件会读取 `_conf_schema.json` 中声明的默认值，并在缺失配置时补齐运行时默认结构。

### 请求相关

- `request_enabled`
- `request_proxies`
- `request_timeout`
- `request_max_retries`

### 输出相关

- `output_base_dir`
- `output_pdf_max_pages`
- `output_jpeg_quality`
- `output_pdf_password`
- `output_max_local_albums`
- `output_max_local_chapters`
- `output_cover_cache_dir`
- `output_cover_cache_max_files`

### 下载并发

- `download_image_threads`
- `download_photo_threads`

### 交互相关

- `interaction_search_page_count_threads`
- `interaction_search_cover_threads`
- `interaction_chapter_detail_threads`
- `chapter_fold_threshold`
- `interaction_max_download_images`
- `interaction_max_download_chapters`
- `interaction_auto_recall_seconds`

### 功能开关

- `features_open_random_search`

## Docker 使用注意事项

如果你使用 Docker 部署，并且 `AstrBot` 与 `NapCat` 运行在不同容器中，需要特别注意文件路径互通问题。

这个插件在发送图片、PDF、转发中的图片节点时，通常会先在 AstrBot 容器内生成本地文件，再交给消息侧读取并发送。
如果 `NapCat` 无法访问这些文件路径，就可能出现以下现象：

- 图片或 PDF 明明已经生成，但发送失败
- 转发消息中的图片无法显示
- AstrBot 容器内路径存在，但 NapCat 容器内路径不存在

因此请确保：

- `AstrBot` 与 `NapCat` 看到的是同一份实际文件
- 两边容器中的挂载目录能够互相对应
- 如果路径不同，需要通过软链接、相同挂载点或其他映射方式打通

例如你当前的环境里，就需要注意这类路径是否实际指向同一份数据：

```text
/opt/1panel/apps/astrbot/astrbot/data ##AstrBot挂载卷
/AstrBot/data
```

如果这两个路径在不同容器里不能互相访问，那么插件即使已经成功生成文件，消息侧也仍然可能拿不到文件。

## 开发参考

- AstrBot 插件开发文档：`https://docs.astrbot.app/dev/star/plugin-new.html`
- AstrBot 插件模板：`https://github.com/Soulter/helloworld`
- AstrBot EH 插件：`https://github.com/drdon1234/astrbot_plugin_ehentai_bot`
- JMComic-Crawler-Python：`https://github.com/hect0x7/JMComic-Crawler-Python`

## 免责声明

本插件仅用于技术研究、接口调试与个人学习，请遵守你所在地区的法律法规，以及目标站点的使用条款。
请勿将本插件用于违规传播、批量搬运或其他不当用途。
