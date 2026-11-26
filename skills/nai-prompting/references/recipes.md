# 实测过的通用配方与构图技巧（SFW 全年龄版）

本文件收录适用于 NovelAI V4.5 / V5 的全年龄构图、多角色控制、分镜、材质与局部细节实测技巧。插件内置 Skill 目录只读，请勿尝试写入新内容到本文件。

---

## 一、写足九层的示范

同一个需求（夏日海边踏浪），只写“人物 + 服装 + 地点 + 动作”容易流于平淡；按 `SKILL.md` 2.5 节写满画风、服装材质、表情、具体场景道具、具名布光、镜头、色调粒子和关键画面瞬间后，立体感与氛围会显著提升：

```text
1girl, solo, mika (blue archive), blue archive,
0.9::artist:shiromanta::, 0.7::artist:karameru::, 0.6::artist:mignon::,
white summer sundress, light chiffon overlay, pink waist ribbon, small pink hair bows, wet hem, barefoot,
laughing, open mouth, blush, sparkling eyes, looking at viewer,
tropical beach, crystal clear turquoise shallow water, white sand seabed, gentle waves, a small white lighthouse on a rocky cape in the distance, towering cumulus clouds, deep blue sky, summer afternoon,
full body, from front, dutch angle, depth of field, blurry foreground,
wading, standing on one leg, kicking, splashing, water splash, sparkle, light particles,
side lighting, warm golden sunlight from the left, rim light on her hair, caustics, sunlight glinting off the water, lens flare,
bright cyan and white palette with soft pink accents,
She kicks up a wide arc of water with her right foot while laughing, and the spray hangs in mid-air around her like a curtain of glass beads catching the sun. Shimmering caustic light ripples across the sand under the clear water, and tiny droplets cling to her eyelashes and the pink ribbons of her dress.
best quality, absurdres, detailed background
```

负面：`nsfw, dark face, bad anatomy, bad hands, extra digits, fewer digits, extra legs, watermark, signature, logo`

> 若使用本插件且开启了「画风槽位」，直接调用工具出图时应省略上面第 2 行的画师串，交由槽位统一注入。

---

## 二、横图（1216×832）防止头顶与兽耳被切

横构图画人物时，人物头顶和兽耳经常顶到画面上沿被切平。要稳住头顶留白，**必须明确写出“耳朵上方那一带画什么具体背景”**，只写“不要切头”往往无效：

- 室内床边：`Her head is in the middle of the frame, not near the top: above the tips of her fox ears there is a wide strip of the bedroom wall with a wooden shelf and a string of warm fairy lights.`
- 窗边站姿全身：`She is seen from across the room and takes up about two thirds of the frame height: above the tips of her fox ears there is a wide strip of the bright window and the curtain rail.`

要点：
- 具体写明耳尖上方那条背景物体（墙面、窗帘杆、挂灯），比笼统写 `head out of frame` 进负面更稳定；
- 尽量不要为了避开上沿而强行把角色框坐标 `y` 压得很低，否则人物容易缩太小或坐卧姿势变形。

---

## 三、左右位置与单侧特征写死

模型能可靠识别 `right` / `left`，但不擅长“把某饰品配给某种颜色的眼睛”这种间接绑定：

- **错误思路**：`her right eye is pink and her left eye is purple, small star mark under her pink eye`（印记容易跑到另一只眼下面）。
- **正确写法**：两句都按左右写死：`her right eye is pink and her left eye is purple, and a small blue star mark sits right under her right eye`。
- 渐变发色/毛色：避免用 `fiery red-orange`（模型可能真画出火焰特效），改为 `gradually turns warm orange-red`，必要时负面补 `fire, flames, burning`。
- 眼下小月牙/水滴状标记：`crescent-shaped mark` 在浅蓝色时容易被画成眼泪，可明确材质或位置并加负面 `tears, crying`。

---

## 四、三人同框与多角色不串色（V5 Full）

三个原创角色（不同发色、发型、发饰）分别用角色框 + 自定义位置（左 `0.3` / 中 `0.5` / 右 `0.7`），主提示词按外观指代（如 `the blonde one on the left...`）：

- **角色框负面别写满其他人的全部特征**：把另外两人的发色、异瞳、发饰全塞进某个角色框的负面，反而容易把本人身上的异瞳或装饰也压掉。
- **正确做法**：角色框负面保持空白，或只放真正容易串过去的那 1~2 个小元素（例如 `braid, braided hair, cheek mark`）。
- **合照拿手机/道具的人**：在多人自拍构图里，模型惯性会让最左边的人伸手臂拿手机；如果要某人拿手机，尽量把她放在画面边侧而不是正中间。
- **只露一只手的人（如画外举手机拍照）**：该角色框**只写手部外观**（如 `a slender hand holding a smartphone, pastel pink nails`），主句里写“她在画面之外”，**不要出现她的发色或头饰**，否则发色和发饰会串到画面里的主角头上。

---

## 五、分镜页与小圆框特写（inset）

- **五格漫画最稳**：
  `comic, multiple panels, manga page layout, clean panel borders, inset` + `This is a 5-panel comic page. The same silver-haired girl appears in every panel. First panel (top, wide): ... Second panel (middle-left): ... Third panel (middle-right): ... Fourth panel (bottom-left): ... Fifth panel (bottom-right): ...`
- **分镜设置要点**：
  1. 负面预设选「不加」（Heavy/Light 带 `multiple views`，会压碎分镜格），手动补 `lowres, worst quality, bad quality, jpeg artifacts, multiple girls`；
  2. 带台词时加 `english text`（或 `chinese text`）与 `A speech bubble says "..."`，并关掉 `Add Quality Tags`，手写 `very aesthetic, masterpiece` 到末尾；
  3. 需要在一个画面角落展示道具或表情细节特写时，在对应格末尾加：`A small circular inset in the corner of this panel shows a close-up of ...`，并在开头标签加入 `inset`。

---

## 六、质感与特殊布料（防透视乱串）

当画面中有一件衣服或材质是半透/薄纱（如透视雪纺外罩、透明雨衣、半透明扇子），**在同一张图里不该透明的衣物一定要显式写明“不透明”**（例如 `opaque white cotton shirt`、`thick opaque denim skirt`），否则半透明属性容易串到整身衣服上。

同理，需要严格控制裙长/袖长等尺寸时，在长提示词中容易被忽略，建议对长度单独轻度加权并用句末锚定：
`1.3::knee-length skirt::, the hem stops right at her knees`。

---

## 七、难点姿势优先策略（同种子对照结论）

遇到难画的特定动作（例如单脚抬高跨步、双人复杂接物动作）：
- 单纯把动作标签挪到主提示词最前面往往提升有限；
- **最有效的办法是双管齐下**：对关键动作标签适度加权（如 `1.3::leg up, kicking::`），同时精简掉抢注意力的无关景物与繁复配饰；先用简洁背景把动作画对，再逐层补回光影与氛围。
- 常见姿势词和低角度机位优先用英文标签（`squatting, from below`），不要全改成长从句。
