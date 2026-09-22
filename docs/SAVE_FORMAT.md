# 存档格式说明

本文记录 iOS 版《元气骑士》本地存档的结构、加密方式和关键字段语义。内容全部来自对
真机存档（版本 8.5.1，iOS 27）的实测，供维护本工具或自行分析时参考。

## 1. 位置

应用数据容器：

```
/var/mobile/Containers/Data/Application/<UUID>/
├── Documents/                                  存档本体
└── Library/Preferences/
    └── com.ChillyRoom.DungeonShooter.plist     设置与解锁状态（二进制 plist）
```

`Documents` 里除了存档分片，还有 `hot_update/`、`Config/`、`YearReview*.json` 等目录和文件，
本工具只读写存档分片与 plist，其余原样带回。

## 2. 分片与加密

| 文件 | 加密 |
| --- | --- |
| `game.data` | 15 字节循环 XOR（密钥 `73 6c 63 7a 7d 67 75 63 7f 57 6d 6c 6b 4a 5f`） |
| `statistic_<UID>_.data` | DES-CBC，密钥 `crst1\0\0\0` |
| `item_data` / `season_data` / `setting` / `task` | DES-CBC，密钥 `iambo\0\0\0` |
| `misc_data` / `bp_data` / `pvp_data` / `monsrise_data` / `escape_season_data` / `mall_reload_data` / `weapon_evolution_data` | 同上（DES-iambo） |
| `sandbox_config` / `sandbox_maps` / `battles` | 明文 JSON |

DES 分片为 CBC 模式、IV 固定 `Ahbool\0\0`、PKCS 填充，密文整体做 base64 后落盘。
部分分片的文件名不带 UID（如 `item_data.data`），少数用单词代替 UID
（`mall_reload_data_LOCAL.data`）。

存档字符串里会出现未转义的控制字符，解析 JSON 时必须 `strict=False`。

## 3. 两种存档格式（最容易踩的坑）

新版本游戏在开启云端存档后会切换到新的 Rijndael 格式：把每个分片额外写成
`*.data.new`，并且只在下面两个开关都为 1 时读写新格式：

```
OpenRijTest_<UID>       = 1
OpenNewtonJsonTest_<UID> = 1
```

这份新格式文件是高熵二进制，本工具不解析它。要让游戏重新读写上表里的经典分片，必须：

1. 把两个开关都置 0（并补齐 `<UID>_first_active` 镜像键）；
2. 在游戏关闭的前提下，把 `*.data.new` 与 `*.data.rij` 移出 `Documents`（本工具会把它们
   移到本地备份和设备的 Media 备份目录里，不会删除）。

**这个开关由服务器在每次游戏会话后重新写回 1**，并存回新格式分片。所以每次改完并进游戏后，
下次再改都要重复上述两步；本工具每次运行时都会自动重做。

## 4. 关键字段

### Documents/game.data

| 字段 | 含义 | 「已拥有」的取值 |
| --- | --- | --- |
| `heroUnlock` | 角色解锁 | `true`（布尔） |
| `petUnlock` | 宠物解锁 | `true` |
| `skinLock` | 角色皮肤，值为 `[{"Name":..., "Value":0/1}]` | `Value: 1`（整数） |
| `heroSkillUnlock` | 技能解锁，值为 `[{"Name":..., "Value":true/false}]` | `Value: true` |
| `heroLevel` | 角色等级 | 上限 8 |
| `gems` / `lastGems` | 宝石 | 目标值，只升不降 |
| `fishChip` | 鱼干 | 目标值 |

### Documents/item_data_<UID>_.data

| 字段 | 含义 | 说明 |
| --- | --- | --- |
| `blueprints` | 图纸表 `key -> "Got" / "Researched"` | **武器厅的解锁状态看的是 `blueprint_weapon_<id>` 是否等于 `Researched`**；`Got` 只代表掉落过图纸 |
| `mythicWeapons` | 神话武器 `[{"id":0-27,"level":0-3}]` | |
| `materials` / `seeds` / `tokenTickets` | 材料/种子/票券，扁平字典 `名字 -> 数量` | |
| `handStyleIndexList` | 手刀样式 | |

武器 id 的两种写法都要覆盖：补零的 `blueprint_weapon_006` 和不补零的 `blueprint_weapon_6`
（不同版本、不同字段用过不同写法）。非数字 id 也存在，例如 `weapon_shower`、`weapon_joker`。
参考存档中还有 `weapon_a/b/c/d`、`weapon_fabric`、`weapon_bossrushfinal(A/B)` 这类特殊 id。

### Documents/weapon_evolution_data_<UID>_.data

```json
{"weapons": {"weapon_006": {"Name": "weapon_006", "Level": 1,
                            "CurrentSkinIndex": 0, "UnlockedSkins": ["weapon_006_s_1"]}}}
```

`Level: 1` 表示已进化。武器皮肤的编号可以从游戏的下载资源
`Documents/hot_update/data/skin/weapon/weapon_<id>/skin_<n>_*.ab` 里读出来。

### Documents/misc_data_<UID>_.data

`unlockedKillEffectIds`：已解锁的击败特效 id 数组，多写几个没有副作用，界面只会列出配置里存在的项。

### Documents/season_data_<UID>_.data

`coin`：赛季币。`gainedCoinInSeason` 等字段只是统计值。

### Library/Preferences/com.ChillyRoom.DungeonShooter.plist

| 键 | 含义 | 类型与取值 |
| --- | --- | --- |
| `<UID>_c<n>_unlock` | 角色解锁 | **字符串** `"True"` |
| `<UID>_c<n>_skin<m>` | 角色皮肤 | **整数** 1 |
| `<UID>_c<n>_level` | 角色等级 | 整数 8 |
| `<UID>_c_<Hero>_skill_<n>_unlock` | 技能 | 整数 1（注意 `_c_` 后面还有一个下划线） |
| `<UID>_p<n>_unlock` | 宠物解锁 | **字符串** `"True"` |
| `<UID>_gems` / `<UID>_last_gems` | 宝石镜像 | 整数 |
| `OpenRijTest_<UID>` / `OpenNewtonJsonTest_<UID>` | 存档格式开关 | 0 |

**类型必须与上面一致**：写成布尔 `true`、整数 `1` 或数字字符串，游戏一律当作未解锁。

## 5. 已知限制

- 武器 id 与名字的对照表在加密的 `luban.bytes` 里，本地读不出来。因此本工具用「编号空间 +
  存档自带的图鉴表 + 存档里已有的图纸键」来覆盖，而不是靠名字匹配。如果新版本又加了新的
  非数字 id，需要在新版本存档里再补齐。
- 云端存档不在本工具的范围内。若账号登录过云存档，游戏在下次会话时可能用云端数据覆盖
  本地修改；建议离线账号使用，或改完后先进入一次游戏确认再登录。
