# 通道适配器必须从第一天设计为多实例

## 规则

新增通道适配器时，**从第一天就设计为多实例（多 bot/多 app）**。不接受"先做单实例，以后再改"。

## 为什么

Telegram 和飞书都踩了同一个坑：
- Telegram：先做单 bot，后来多项目需要多 bot → 改 config 模型 + adapter 注册逻辑 + 路由 key 格式
- 飞书：先做单 app，后来 smart_trade 需要新飞书 app → 改 config + adapter + 还撞上 lark SDK 全局 event loop 竞态

两次改造的实际工作量都不大（config 加列表 + main.py 加循环），但**改造本身引入了新 bug**（飞书 race condition、配置格式兼容）并消耗了排查时间。如果一开始就是多实例，这些成本为零。

## 设计模板

```python
# config.yaml
channels:
  <channel_type>:
    instances:
    - instance_id: "xxx"
      credential_1: "..."
      credential_2: "..."
    - instance_id: "yyy"
      credential_1: "..."

# main.py adapter 注册
for cfg in config.channels.<channel_type>.instances:
    adapter = <Channel>Adapter(cfg.instance_id, cfg.credential_1, ...)
    adapters[f"<channel_type>:{cfg.instance_id}"] = adapter

# 路由
routes:
  - channel: <channel_type>
    bot_id: "xxx"  # = instance_id
    chat_id: "..."
    backend: my-agent
```

## 当前状态

| 适配器 | 多实例支持 | 状态 |
|--------|-----------|------|
| Telegram | 是（telegram_bots 列表） | 已完成 |
| 飞书 | 是（feishu_apps 列表） | 已完成 |
| HTTP | 不适用（无状态，直连 backend） | — |
