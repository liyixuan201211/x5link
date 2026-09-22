// 拖动方向的回归测试 —— 断言的是「画面往哪边走」，不是「视线往哪转」。
//
// 上一版测错了对象：我断言了「手向上拖 -> 视线朝上」，于是代码写成抬头，
// 结果画面往下走，手和画面反着来。用户看到的永远是**画面位移**，
// 所以这里改成把固定的世界坐标点投影到屏幕上，看它被拖到哪儿去了。
//
// 业界标准（街景 / YouTube 360 / Pannellum / Photo Sphere Viewer / egjs view360）：
//   「抓住画面拖」—— 画面位移方向 == 手移动方向。
//
// 跑：node tools/test_view_dir.mjs

function norm(v) { const n = Math.hypot(...v); return v.map(x => x / n); }

// shader 的正向：屏幕 NDC -> 世界方向（与 viewer.html 的 fragment shader 一致）
function viewDir(yaw, pitch, fov, px, py) {
  const f = 1 / Math.tan(fov / 2);
  let d = norm([px, py, -f]);
  const cp = Math.cos(pitch), sp = Math.sin(pitch);
  d = [d[0], cp * d[1] - sp * d[2], sp * d[1] + cp * d[2]];
  const cy = Math.cos(yaw), sy = Math.sin(yaw);
  return [cy * d[0] + sy * d[2], d[1], -sy * d[0] + cy * d[2]];
}

// shader 的逆：世界方向 -> 屏幕 NDC（+x 右，+y 上）。
// 用来问「这个地标现在出现在屏幕的哪个位置」。
function project(yaw, pitch, fov, w) {
  const f = 1 / Math.tan(fov / 2);
  let v = w.slice();
  let c = Math.cos(-yaw), s = Math.sin(-yaw);          // Ry(-yaw)
  v = [c * v[0] + s * v[2], v[1], -s * v[0] + c * v[2]];
  c = Math.cos(-pitch); s = Math.sin(-pitch);          // Rx(-pitch)
  v = [v[0], c * v[1] - s * v[2], s * v[1] + c * v[2]];
  if (v[2] >= -1e-6) return null;                      // 在身后，看不见
  return { x: -f * v[0] / v[2], y: -f * v[1] / v[2] };
}

// 与 viewer.html 的 pointermove 一致
function drag(st, dxPx, dyPx, cssW, cssH, canvasW, canvasH) {
  const fovX = 2 * Math.atan(Math.tan(st.fov / 2) * (canvasW / canvasH));
  return { ...st,
           yaw:   st.yaw   + dxPx * (fovX / cssW),
           pitch: st.pitch + dyPx * (st.fov / cssH) };
}

const FOV = 75 * Math.PI / 180;
const deg = r => (r * 180 / Math.PI).toFixed(2) + '°';
let pass = 0, fail = 0;
function check(name, cond, extra = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}${extra ? '   ' + extra : ''}`);
  cond ? pass++ : fail++;
}

// 两个地标：一个偏上、一个偏右，用来观察画面位移
const LANDMARK_UP    = norm([0, 1, -3]);
const LANDMARK_RIGHT = norm([1, 0, -3]);

const s = { yaw: 0, pitch: 0, fov: FOV };
const W = 1000, H = 600;   // CSS 尺寸（视口）

console.log('画面位移方向（这是用户唯一能感知的东西）');

// 手向上拖 -> 画面里的地标应该往上走
{
  const before = project(s.yaw, s.pitch, s.fov, LANDMARK_UP);
  const after = drag(s, 0, -100, W, H, W, H);
  const now = project(after.yaw, after.pitch, after.fov, LANDMARK_UP);
  check('手向上拖 -> 画面向上走（图片黏住鼠标）',
        now.y > before.y, `屏幕 y: ${before.y.toFixed(3)} -> ${now.y.toFixed(3)}`);
}

// 手向下拖 -> 画面往下走
{
  const before = project(s.yaw, s.pitch, s.fov, LANDMARK_UP);
  const after = drag(s, 0, +100, W, H, W, H);
  const now = project(after.yaw, after.pitch, after.fov, LANDMARK_UP);
  check('手向下拖 -> 画面向下走',
        now.y < before.y, `屏幕 y: ${before.y.toFixed(3)} -> ${now.y.toFixed(3)}`);
}

// 手向右拖 -> 画面往右走
{
  const before = project(s.yaw, s.pitch, s.fov, LANDMARK_RIGHT);
  const after = drag(s, +100, 0, W, H, W, H);
  const now = project(after.yaw, after.pitch, after.fov, LANDMARK_RIGHT);
  check('手向右拖 -> 画面向右走',
        now.x > before.x, `屏幕 x: ${before.x.toFixed(3)} -> ${now.x.toFixed(3)}`);
}

// 手向左拖 -> 画面往左走
{
  const before = project(s.yaw, s.pitch, s.fov, LANDMARK_RIGHT);
  const after = drag(s, -100, 0, W, H, W, H);
  const now = project(after.yaw, after.pitch, after.fov, LANDMARK_RIGHT);
  check('手向左拖 -> 画面向左走',
        now.x < before.x, `屏幕 x: ${before.x.toFixed(3)} -> ${now.x.toFixed(3)}`);
}

// 反向验证：如果写成「抬头/右转」那种（手和画面反着走），上面几条必须失败
console.log('\n反向验证（确认测试真的能抓到错误写法）');
{
  const wrong = { ...drag(s, 0, -100, W, H, W, H) };
  wrong.pitch = s.pitch - (-100) * (s.fov / H);   // 上一版的错误写法
  const now = project(wrong.yaw, wrong.pitch, wrong.fov, LANDMARK_UP);
  const before = project(s.yaw, s.pitch, s.fov, LANDMARK_UP);
  check('错误写法会被判失败（画面反而往下走）', now.y < before.y,
        `屏幕 y: ${before.y.toFixed(3)} -> ${now.y.toFixed(3)}`);
}

console.log('\n灵敏度（转过的角度 ∝ 当前视场角，所以缩放后手感一致）');
{
  const fullH = drag(s, 0, -H, W, H, W, H);
  check('拖满视口高度 = 正好转过 1 个垂直 FOV',
        Math.abs(Math.abs(fullH.pitch) - FOV) < 1e-9,
        `${deg(fullH.pitch)} vs FOV ${deg(FOV)}（向上拖满 = 视线下转一整个 FOV，正常）`);

  const fovX = 2 * Math.atan(Math.tan(FOV / 2) * (W / H));
  const fullW = drag(s, W, 0, W, H, W, H);
  check('拖满视口宽度 = 正好转过 1 个水平 FOV',
        Math.abs(fullW.yaw - fovX) < 1e-9,
        `${deg(fullW.yaw)} vs ${deg(fovX)}`);

  const zoomed = { ...s, fov: 30 * Math.PI / 180 };
  const z = drag(zoomed, 0, -100, W, H, W, H);
  const n = drag(s, 0, -100, W, H, W, H);
  check('放大(30°)后同样拖动幅度位移更小，不会一拖就飞',
        Math.abs(z.pitch) < Math.abs(n.pitch),
        `${deg(z.pitch)} < ${deg(n.pitch)}`);
}

console.log('\n经纬图映射（确认没有镜像）');
{
  const right = viewDir(0, 0, FOV, 1, 0);
  const lonR = Math.atan2(right[0], -right[2]);
  check('屏幕右侧 -> 经纬图 u > 0.5（经度方向一致）', lonR > 0, `lon=${deg(lonR)}`);
  const upDir = viewDir(0, 1, FOV, 0, 1);
  const latU = Math.asin(Math.max(-1, Math.min(1, upDir[1])));
  check('屏幕上方 -> 纬度为正（对应图上半部）', latU > 0, `lat=${deg(latU)}`);
}

console.log('\n' + '='.repeat(60));
if (fail) { console.log(`结果：${fail} 项失败`); process.exit(1); }
console.log(`结果：${pass} 项全部通过 —— 画面与手同向。`);
