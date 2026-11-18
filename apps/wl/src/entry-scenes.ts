// Procedural entry models, after short ASCII-art reels: built from low-poly Three.js shapes
// instead of a .glb, and animated over time, in colour. The balance between the busy originals and
// a light page: one cached material per colour, and anything that repeats — sparkles, snow,
// ornaments, sprinkles — is one InstancedMesh, so a few hundred specks cost a single draw call.
// Nothing is allocated per frame.
// build() returns the full-size object (so fitting sees its final size) and update(t), called
// every frame with the seconds since the model was shown.

type Three = typeof import("three");
type Object3D = import("three").Object3D;
type Geometry = import("three").BufferGeometry;
export type Animated = { object: Object3D; ambient?: Object3D; update: (t: number) => void };

const smooth = (v: number) => { const x = Math.min(1, Math.max(0, v)); return x * x * (3 - 2 * x); };
const grown = (v: number) => Math.max(0.001, smooth(v));  // scale 0 breaks normals in the shading
const GOLDEN = 2.39996;
const WHITE = 0xffffff;
const REDS = [0xc81e2d, 0xe1323c, 0xaa1428, 0xeb6e82, 0xd75064];
const PARTY = [0xff5a5a, 0xffd23c, 0x5ac8ff, 0x8cff6e, 0xff8ce6];
const STEM = 0x2f8a45, LEAF = 0x3cbe50, GOLD = 0xf0be3c, SILVER = 0xd8dce4, COAL = 0x2a2f3a;
const hash = (i: number) => { const x = Math.sin(i * 127.1) * 43758.5453; return x - Math.floor(x); };

function kit(THREE: Three) {
  const materials = new Map<number, import("three").Material>();
  const mat = (color: number) => {
    if (!materials.has(color)) materials.set(color, new THREE.MeshStandardMaterial({ color, roughness: 0.6, flatShading: true, side: THREE.DoubleSide }));
    return materials.get(color)!;
  };
  const dummy = new THREE.Object3D();
  const tint = new THREE.Color();
  // Sparkles, snow and confetti live here: added after fitting, so they don't shrink the model.
  const ambient = new THREE.Group();
  const mesh = (geometry: Geometry, parent: Object3D, x = 0, y = 0, z = 0, color = WHITE) => {
    const m = new THREE.Mesh(geometry, mat(color));
    m.position.set(x, y, z);
    parent.add(m);
    return m;
  };
  // Many copies of one small shape, one draw call; place(i, t, d) poses copy i. A colour list
  // is cycled through the copies.
  const many = (geometry: Geometry, count: number, parent: Object3D, colors: number | number[], place: (i: number, t: number, d: Object3D) => void) => {
    const list = Array.isArray(colors) ? colors : [colors];
    const m = new THREE.InstancedMesh(geometry, mat(list.length > 1 ? WHITE : list[0]), count);
    m.frustumCulled = false;
    if (list.length > 1) for (let i = 0; i < count; i++) m.setColorAt(i, tint.setHex(list[i % list.length]));
    parent.add(m);
    const update = (t: number) => {
      for (let i = 0; i < count; i++) {
        dummy.position.set(0, 0, 0); dummy.rotation.set(0, 0, 0); dummy.scale.setScalar(1);
        place(i, t, dummy);
        dummy.updateMatrix();
        m.setMatrixAt(i, dummy.matrix);
      }
      m.instanceMatrix.needsUpdate = true;
    };
    update(99);  // full size for fitting
    return update;
  };
  // Twinkling specks in a shell around the model, like the stars in the reels.
  const speck = new THREE.OctahedronGeometry(0.018, 0);
  const sparkles = (count: number, radius: number, colors: number | number[] = [WHITE, 0xfff2c0]) => many(speck, count, ambient, colors, (i, t, d) => {
    const a = i * GOLDEN, y = hash(i) * 2 - 1, r = radius * (0.75 + 0.35 * hash(i + 7)), ring = Math.sqrt(1 - y * y);
    d.position.set(Math.cos(a) * r * ring, y * r, Math.sin(a) * r * ring);
    d.scale.setScalar(Math.max(0.001, Math.sin(t * (1.5 + hash(i + 3) * 2) + i)) * smooth(t - 0.5));
  });
  return { mesh, many, sparkles, ambient };
}

export function christmasTree(THREE: Three): Animated {
  const { mesh, many, ambient } = kit(THREE);
  const tree = new THREE.Group();
  const TIERS = 6, TOP = 1.15, BOTTOM = -0.75, step = (TOP - BOTTOM - 0.3) / TIERS;
  const radius = (i: number) => 0.2 + 0.72 * ((i + 1) / TIERS);
  const tiers = Array.from({ length: TIERS }, (_, i) => {
    const cone = mesh(new THREE.ConeGeometry(radius(i), 0.55, 12, 2), tree, 0, TOP - 0.25 - i * step, 0, 0x2f9a4a);
    cone.rotation.y = i * 0.7;
    return cone;
  });
  const trunk = mesh(new THREE.CylinderGeometry(0.1, 0.13, 0.35, 6), tree, 0, BOTTOM - 0.12, 0, 0x7a4b23);
  const star = mesh(new THREE.OctahedronGeometry(0.14, 0), tree, 0, TOP + 0.14, 0, 0xffc43c);
  // Ornaments and a garland of lights spiralling down, each one instanced mesh.
  const ornaments = many(new THREE.IcosahedronGeometry(0.05, 0), 48, tree, [0xe62828, 0xffc43c, 0x3c6eff, 0xf0f0ff], (i, t, d) => {
    const tier = i % TIERS, a = i * GOLDEN, r = radius(tier) * 0.82;
    d.position.set(Math.cos(a) * r, tiers[tier].position.y - 0.18, Math.sin(a) * r);
    d.scale.setScalar(smooth((t - 4.5 - i * 0.03) / 0.5) * (0.75 + 0.35 * Math.sin(t * 3 + i * 1.7)) + 0.001);
  });
  const garland = many(new THREE.OctahedronGeometry(0.022, 0), 120, tree, [0xffe08a, 0xff6a6a, 0x9fd0ff], (i, t, d) => {
    const u = i / 120, y = TOP - 0.1 - u * (TOP - BOTTOM - 0.1), r = 0.12 + 0.8 * u;
    d.position.set(Math.cos(u * 26) * r, y, Math.sin(u * 26) * r);
    d.scale.setScalar(smooth((t - 4 - u * 1.5) / 0.3) * (Math.sin(t * 6 - i * 0.5) > 0 ? 1 : 0.4) + 0.001);
  });
  const snow = many(new THREE.OctahedronGeometry(0.016, 0), 90, ambient, WHITE, (i, t, d) => {
    d.position.set((hash(i) - 0.5) * 3, 1.5 - ((t * 0.15 + hash(i + 5) * 3) % 3), (hash(i + 9) - 0.5) * 2);
  });
  const gifts = [[-0.72, 0.3, 0.3, 0x3c6eff], [0.72, 0.15, 0.34, 0xe62828], [0.32, 0.6, 0.24, 0xe62828], [-0.3, 0.62, 0.22, 0xffc43c]].map(([x, z, w, color]) => {
    const g = new THREE.Group();
    mesh(new THREE.BoxGeometry(w, w * 0.8, w), g, 0, 0, 0, color);
    mesh(new THREE.TorusGeometry(w * 0.15, w * 0.05, 3, 8), g, 0, w * 0.45, 0, color === 0xffc43c ? 0xe62828 : 0xffc43c);  // bow
    g.position.set(x, BOTTOM - 0.3 + w * 0.4, z);
    tree.add(g);
    return g;
  });
  return {
    object: tree,
    ambient,
    update(t) {
      const grow = t / 4;
      tiers.forEach((tier, i) => tier.scale.setScalar(grown(grow * TIERS - i)));
      trunk.scale.setScalar(grown(grow * TIERS - TIERS));
      star.scale.setScalar(grown(grow * 3) * (1 + 0.15 * Math.sin(t * 4)));
      star.rotation.y = t * 1.5;
      ornaments(t); garland(t); snow(t);
      gifts.forEach((g, i) => g.scale.setScalar(grown((t - 5.5 - i * 0.3) / 0.8)));
    },
  };
}

// Cupped petals in a spiral; open (0..1) tilts them outwards. Shared by both rose scenes.
function roseHead(THREE: Three, mesh: ReturnType<typeof kit>["mesh"], layers: number, size: number, turn: number, color: number) {
  const head = new THREE.Group();
  const petals: { holder: Object3D; layer: number }[] = [];
  for (let layer = 0; layer < layers; layer++) {
    const count = 3 + layer, s = size * (0.5 + layer * 0.3);
    const geometry = new THREE.SphereGeometry(s, 6, 3, 0, Math.PI * 0.7, 0, Math.PI * 0.5);
    for (let p = 0; p < count; p++) {
      const spin = new THREE.Group();
      spin.rotation.y = (p / count) * Math.PI * 2 + layer * 0.6 + turn;
      const holder = new THREE.Group();
      spin.add(holder);
      mesh(geometry, holder, 0, 0, s * 0.3, color).rotation.x = -Math.PI / 2;
      head.add(spin);
      petals.push({ holder, layer });
    }
  }
  const open = (k: number) => { for (const { holder, layer } of petals) holder.rotation.x = (0.1 + layer * 0.13) * (0.3 + 1.6 * k); };
  return { head, open };
}

const growTube = (geometry: Geometry, k: number) => geometry.setDrawRange(0, Math.floor(((geometry.index?.count ?? 0) * k) / 6) * 6);

export function roseBouquet(THREE: Three): Animated {
  const { mesh, many, sparkles, ambient } = kit(THREE);
  const bouquet = new THREE.Group();
  const N = 16, WAIST = new THREE.Vector3(0, -0.45, 0);
  const stems: Geometry[] = [];
  const tube = (curve: import("three").Curve<import("three").Vector3>) => {
    const geometry = new THREE.TubeGeometry(curve, 10, 0.015, 4);
    stems.push(geometry);
    mesh(geometry, bouquet, 0, 0, 0, STEM);
  };
  for (let i = 0; i < 5; i++) tube(new THREE.LineCurve3(new THREE.Vector3((i - 2) * 0.05, -1.1, 0), WAIST));
  const roses = Array.from({ length: N }, (_, k) => {
    const u = (k + 0.5) / N, theta = Math.acos(1 - u * 0.75), phi = k * GOLDEN;
    const pos = new THREE.Vector3(Math.sin(theta) * Math.cos(phi) * 0.95, 0.15 + Math.cos(theta) * 0.5, Math.sin(theta) * Math.sin(phi) * 0.65);
    tube(new THREE.QuadraticBezierCurve3(WAIST, new THREE.Vector3(pos.x * 0.3, (WAIST.y + pos.y) / 2, pos.z * 0.3), pos));
    const rose = roseHead(THREE, mesh, 4, 0.065, k, REDS[k % REDS.length]);
    rose.head.position.copy(pos);
    rose.head.lookAt(pos.x * 2, pos.y * 2 + 1, pos.z * 2);
    rose.head.rotateX(Math.PI / 2);
    bouquet.add(rose.head);
    return { ...rose, t0: 2 + k * 0.35 };
  });
  const leaf = new THREE.SphereGeometry(0.12, 4, 3);
  const leaves = Array.from({ length: 16 }, (_, i) => {
    const a = i * GOLDEN, m = mesh(leaf, bouquet, Math.cos(a) * 0.9, -0.15 + (i % 5) * 0.14, Math.sin(a) * 0.58, LEAF);
    m.rotation.set(0.4, -a, 0.9);
    return m;
  });
  const bow = new THREE.Group();
  mesh(new THREE.TorusGeometry(0.1, 0.03, 4, 12), bow, 0, 0, 0, GOLD).rotation.x = Math.PI / 2;
  for (const side of [-1, 1]) mesh(new THREE.TorusGeometry(0.07, 0.02, 4, 10), bow, side * 0.09, 0.02, 0.1, GOLD).rotation.y = side * 0.5;
  bow.position.copy(WAIST);
  bouquet.add(bow);
  const tFall = 2 + N * 0.35 + 1;
  const falling = many(new THREE.IcosahedronGeometry(0.03, 0), 14, bouquet, REDS, (i, t, d) => {
    const age = t - tFall - i * 0.6;
    if (age <= 0 || t > 90) { d.scale.setScalar(0.001); return; }
    const from = roses[(i * 3) % N].head.position, a = age % 6;
    d.position.set(from.x + Math.sin(i * 7.1) * 0.25 * a + 0.06 * Math.sin(a * 3), from.y - a * 0.35, from.z + 0.1);
    d.rotation.set(a * 2, a, 0);
    d.scale.set(1, 0.35, 1);
  });
  const stars = sparkles(70, 1.5);
  return {
    object: bouquet,
    ambient,
    update(t) {
      for (const g of stems) growTube(g, smooth(t / 2));
      leaves.forEach((l, i) => { const k = grown((t - 1.8 - i * 0.06) / 0.6); l.scale.set(0.35 * k, k, 0.08); });
      bow.scale.setScalar(grown((t - 1.6) / 0.6));
      for (const { head, open, t0 } of roses) { head.scale.setScalar(grown((t - t0 + 0.6) / 0.6)); open(smooth((t - t0) / 1.2)); }
      falling(t); stars(t);
    },
  };
}

export function heart(THREE: Three): Animated {
  const { mesh, many, sparkles, ambient } = kit(THREE);
  const shape = new THREE.Shape();
  shape.moveTo(0, -0.9);
  shape.bezierCurveTo(-0.3, -0.55, -1, -0.25, -1, 0.3);
  shape.bezierCurveTo(-1, 0.75, -0.6, 0.95, -0.35, 0.95);
  shape.bezierCurveTo(-0.15, 0.95, 0, 0.8, 0, 0.6);
  shape.bezierCurveTo(0, 0.8, 0.15, 0.95, 0.35, 0.95);
  shape.bezierCurveTo(0.6, 0.95, 1, 0.75, 1, 0.3);
  shape.bezierCurveTo(1, -0.25, 0.3, -0.55, 0, -0.9);
  const geometry = new THREE.ExtrudeGeometry(shape, { depth: 0.35, bevelEnabled: true, bevelSize: 0.12, bevelThickness: 0.12, bevelSegments: 3, curveSegments: 12 });
  geometry.center();
  const group = new THREE.Group();
  const body = mesh(geometry, group, 0, 0, 0, 0xc77dba);
  // The reel's heart is filled with words; here a halo of small hearts orbits it instead.
  const outline = shape.getSpacedPoints(60);
  const halo = many(new THREE.OctahedronGeometry(0.03, 0), outline.length, group, [0xf6a6dc, 0xd88bff], (i, t, d) => {
    const p = outline[i], k = 1.25 + 0.05 * Math.sin(t * 3 + i * 0.4);
    d.position.set(p.x * k, p.y * k - 0.02, 0);
    d.scale.setScalar(smooth((t - 0.8 - i * 0.02) / 0.3) + 0.001);
  });
  const stars = sparkles(60, 1.6, [WHITE, 0xf6a6dc]);
  return {
    object: group,
    ambient,
    update(t) {
      const beat = t % 1.1;  // lub-dub: two quick swells, then a rest
      const k = 1 + 0.08 * Math.exp(-beat * 9) + (beat > 0.25 ? 0.05 * Math.exp(-(beat - 0.25) * 9) : 0);
      body.scale.setScalar(grown(t / 0.8) * k);
      halo(t); stars(t);
    },
  };
}

export function ocean(THREE: Three): Animated {
  const { mesh, many, ambient } = kit(THREE);
  const SEG = 48;
  const geometry = new THREE.PlaneGeometry(3.2, 3.2, SEG, SEG);
  geometry.rotateX(-Math.PI / 2);
  const pos = geometry.attributes.position as import("three").BufferAttribute;
  const base = Float32Array.from(pos.array as Float32Array);
  const group = new THREE.Group();
  const sea = mesh(geometry, group, 0, 0, 0, 0x3a6f9a);
  const moon = mesh(new THREE.SphereGeometry(0.22, 12, 8), group, 0.9, 0.9, -1.5, 0xf2f0dc);
  const wave = (x: number, z: number, t: number) =>
    0.12 * Math.sin(x * 2.1 + t * 1.4) + 0.08 * Math.sin(z * 3.3 - t * 1.9 + x) + 0.05 * Math.sin((x + z) * 5 + t * 2.6);
  // Foam riding the crests.
  const foam = many(new THREE.OctahedronGeometry(0.02, 0), 160, group, 0xe8f4ff, (i, t, d) => {
    const x = (hash(i) - 0.5) * 3, z = (hash(i + 3) - 0.5) * 3, y = wave(x, z, t);
    d.position.set(x, y + 0.03, z);
    d.scale.setScalar(y > 0.08 ? 1 : 0.001);
  });
  group.rotation.x = 0.35;  // look across the water towards the horizon
  return {
    object: group,
    ambient,
    update(t) {
      for (let i = 0; i < pos.count; i++) pos.setY(i, wave(base[i * 3], base[i * 3 + 2], t));
      pos.needsUpdate = true;
      geometry.computeVertexNormals();
      sea.scale.y = grown(t);
      moon.scale.setScalar(grown(t - 1));
      foam(t);
    },
  };
}

export function blackHole(THREE: Three): Animated {
  const { mesh, many, sparkles, ambient } = kit(THREE);
  const group = new THREE.Group();
  group.add(new THREE.Mesh(new THREE.SphereGeometry(0.42, 16, 10), new THREE.MeshBasicMaterial({ color: 0x000000 })));
  const tilt = new THREE.Group();
  tilt.rotation.x = -Math.PI / 2 + 0.25;
  group.add(tilt);
  // Banded accretion disk: three rings turning at different speeds.
  const bands = [[0.55, 0.8, 0xffd28a], [0.85, 1.05, 0xffa040], [1.1, 1.35, 0xb8602a]].map(([a, b, c]) => mesh(new THREE.RingGeometry(a, b, 40, 1), tilt, 0, 0, 0, c));
  const halo = mesh(new THREE.TorusGeometry(0.5, 0.045, 4, 36), group, 0, 0, 0, 0xffe6b0);  // photon ring, facing the viewer
  const dust = many(new THREE.OctahedronGeometry(0.02, 0), 150, tilt, [0xffd28a, 0xffa040, 0xfff0d0], (i, t, d) => {
    const r = 0.5 + 1.1 * hash(i), a = i * GOLDEN + t * (1.2 / r);  // inner dust orbits faster
    d.position.set(Math.cos(a) * r, Math.sin(a) * r, (hash(i + 2) - 0.5) * 0.05);
  });
  const stars = sparkles(70, 1.8, [WHITE, 0xffe6b0]);
  return {
    object: group,
    ambient,
    update(t) {
      const k = grown(t / 2);
      bands.forEach((b, i) => { b.rotation.z = t * (0.9 - i * 0.25); b.scale.setScalar(grown((t - i * 0.3) / 1.5)); });
      halo.scale.setScalar(k * (1 + 0.04 * Math.sin(t * 5)));
      dust(t); stars(t);
    },
  };
}

export function cake(THREE: Three): Animated {
  const { mesh, many, sparkles, ambient } = kit(THREE);
  const group = new THREE.Group();
  const plate = mesh(new THREE.CylinderGeometry(1.05, 1.05, 0.05, 24), group, 0, -0.62, 0, 0xdadde6);
  const tiers = [[0.85, 0.5, -0.35], [0.6, 0.4, 0.1]].map(([r, h, y]) => mesh(new THREE.CylinderGeometry(r, r, h, 24), group, 0, y, 0, 0xc8843c));
  const rims = [[0.85, -0.1], [0.6, 0.3]].map(([r, y]) => { const m = mesh(new THREE.TorusGeometry(r, 0.05, 4, 24), group, 0, y, 0, 0xfff1d6); m.rotation.x = Math.PI / 2; return m; });
  // Icing drips hanging off each rim.
  const drip = new THREE.CapsuleGeometry(0.035, 0.08, 2, 4);
  const drips = many(drip, 26, group, 0xfff1d6, (i, t, d) => {
    const top = i < 16, a = (i / (top ? 16 : 10)) * Math.PI * 2, r = top ? 0.86 : 0.61;
    d.position.set(Math.cos(a) * r, (top ? -0.17 : 0.23) - hash(i) * 0.04, Math.sin(a) * r);
    d.scale.set(1, (0.6 + hash(i + 1) * 0.8) * smooth((t - 1.6 - i * 0.02) / 0.4) + 0.001, 1);
  });
  const candle = mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.35, 6), group, 0, 0.47, 0, 0x7ab8ff);
  const flame = mesh(new THREE.ConeGeometry(0.06, 0.18, 6), group, 0, 0.75, 0, 0xffb030);
  const sprinkles = many(new THREE.BoxGeometry(0.05, 0.02, 0.02), 40, group, PARTY, (i, t, d) => {
    const a = i * GOLDEN, r = 0.12 + hash(i) * 0.42;
    d.position.set(Math.cos(a) * r, 0.31, Math.sin(a) * r);
    d.rotation.y = a * 3;
    d.scale.setScalar(smooth((t - 1.8 - i * 0.02) / 0.3) + 0.001);
  });
  const confetti = many(new THREE.PlaneGeometry(0.05, 0.03), 60, ambient, PARTY, (i, t, d) => {
    const fall = (t * 0.25 + hash(i)) % 1;
    d.position.set((hash(i + 4) - 0.5) * 2.6, 1.4 - fall * 2.6, (hash(i + 8) - 0.5) * 1.6);
    d.rotation.set(t * 3 + i, t * 2, 0);
    d.scale.setScalar(smooth((t - 3.2) / 0.5) + 0.001);
  });
  const stars = sparkles(40, 1.7);
  return {
    object: group,
    ambient,
    update(t) {
      plate.scale.setScalar(grown(t / 0.5));
      tiers.forEach((m, i) => m.scale.setScalar(grown((t - 0.4 - i * 0.6) / 0.6)));
      rims.forEach((m, i) => m.scale.setScalar(grown((t - 1.2 - i * 0.2) / 0.4)));
      drips(t); sprinkles(t); confetti(t); stars(t);
      candle.scale.setScalar(grown((t - 2.4) / 0.5));
      const lit = grown((t - 3) / 0.4);
      flame.scale.set(lit * (1 + 0.15 * Math.sin(t * 13)), lit * (1 + 0.25 * Math.sin(t * 9 + 1)), lit);
      flame.rotation.z = 0.15 * Math.sin(t * 7);
    },
  };
}

export function rose(THREE: Three): Animated {
  const { mesh, many, sparkles, ambient } = kit(THREE);
  const group = new THREE.Group();
  const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(0, -1.1, 0), new THREE.Vector3(0.12, -0.3, 0), new THREE.Vector3(0, 0.45, 0));
  const stem = new THREE.TubeGeometry(curve, 20, 0.03, 5);
  mesh(stem, group, 0, 0, 0, STEM);
  const thorns = many(new THREE.ConeGeometry(0.02, 0.06, 4), 7, group, 0x2a7a3a, (i, t, d) => {
    const u = 0.15 + i * 0.1, p = curve.getPoint(u), side = i % 2 ? 1 : -1;
    d.position.set(p.x + side * 0.03, p.y, p.z);
    d.rotation.z = side * -1.2;
    d.scale.setScalar(smooth((t - u * 2) / 0.3) + 0.001);
  });
  const leaf = new THREE.SphereGeometry(0.2, 5, 3);
  const leaves = [[-1, -0.55], [1, -0.2]].map(([side, y]) => { const m = mesh(leaf, group, side * 0.17, y, 0, LEAF); m.rotation.z = side * -0.9; return m; });
  const sepals = Array.from({ length: 5 }, (_, i) => {
    const m = mesh(new THREE.ConeGeometry(0.03, 0.14, 3), group, Math.cos(i * 1.257) * 0.06, 0.4, Math.sin(i * 1.257) * 0.06, LEAF);
    m.rotation.set(Math.sin(i * 1.257) * 2.2, 0, -Math.cos(i * 1.257) * 2.2);
    return m;
  });
  const bud = roseHead(THREE, mesh, 5, 0.11, 0, 0xd0202e);
  bud.head.position.y = 0.45;
  group.add(bud.head);
  const stars = sparkles(60, 1.4);
  return {
    object: group,
    ambient,
    update(t) {
      growTube(stem, smooth(t / 2));
      thorns(t);
      leaves.forEach((l, i) => { const k = grown((t - 1 - i * 0.5) / 0.6); l.scale.set(0.35 * k, k, 0.1); });
      sepals.forEach((s) => s.scale.setScalar(grown((t - 1.7) / 0.4)));
      bud.head.scale.setScalar(grown((t - 1.8) / 0.6));
      bud.open(smooth((t - 2.4) / 2.5));
      stars(t);
    },
  };
}

export function ring(THREE: Three): Animated {
  const { mesh, many, sparkles, ambient } = kit(THREE);
  const group = new THREE.Group();
  const band = mesh(new THREE.TorusGeometry(0.7, 0.1, 8, 40), group, 0, 0, 0, SILVER);
  const head = new THREE.Group();
  head.position.y = 0.82;
  group.add(head);
  mesh(new THREE.CylinderGeometry(0.14, 0.08, 0.15, 8), head, 0, 0, 0, SILVER);
  for (let i = 0; i < 4; i++) {  // prongs holding the stone
    const a = i * Math.PI / 2 + Math.PI / 4;
    mesh(new THREE.CylinderGeometry(0.015, 0.02, 0.28, 4), head, Math.cos(a) * 0.13, 0.15, Math.sin(a) * 0.13, SILVER).rotation.set(Math.sin(a) * 0.3, 0, -Math.cos(a) * 0.3);
  }
  const gem = mesh(new THREE.OctahedronGeometry(0.26, 0), head, 0, 0.23, 0, 0xe8f4ff);
  gem.scale.y = 0.8;
  const sideStones = many(new THREE.OctahedronGeometry(0.045, 0), 8, group, 0xe8f4ff, (i, t, d) => {
    const a = Math.PI / 2 + (i < 4 ? -1 : 1) * (0.28 + (i % 4) * 0.12);
    d.position.set(Math.cos(a) * 0.7, Math.sin(a) * 0.7 + 0.08, 0);
    d.scale.setScalar(smooth((t - 1.4 - (i % 4) * 0.1) / 0.3) + 0.001);
  });
  // Glints flashing around the stone.
  const glints = many(new THREE.OctahedronGeometry(0.03, 0), 12, head, WHITE, (i, t, d) => {
    const a = i * GOLDEN, flash = Math.max(0, Math.sin(t * 2.5 + i * 1.3));
    d.position.set(Math.cos(a) * 0.35, 0.25 + Math.sin(a * 2) * 0.2, Math.sin(a) * 0.35);
    d.scale.set(flash ** 4 * 0.6 + 0.001, flash ** 4 * 3 + 0.001, flash ** 4 * 0.6 + 0.001);
  });
  const stars = sparkles(50, 1.6);
  return {
    object: group,
    ambient,
    update(t) {
      band.scale.setScalar(grown(t / 1.2));
      head.scale.setScalar(grown((t - 0.8) / 0.6));
      gem.rotation.y = t * 1.2;
      sideStones(t); glints(t); stars(t);
    },
  };
}

export function snowman(THREE: Three): Animated {
  const { mesh, many, ambient } = kit(THREE);
  const group = new THREE.Group();
  const body = new THREE.Group();
  group.add(body);
  const parts = [
    mesh(new THREE.BoxGeometry(0.9, 0.75, 0.7), body, 0, -0.6),
    mesh(new THREE.BoxGeometry(0.72, 0.6, 0.6), body, 0, 0.07),
    mesh(new THREE.BoxGeometry(0.58, 0.55, 0.52), body, 0, 0.66),
  ];
  const extras = new THREE.Group();  // hat, scarf, nose, face, arms
  body.add(extras);
  mesh(new THREE.BoxGeometry(0.7, 0.05, 0.62), extras, 0, 0.95, 0, COAL);
  mesh(new THREE.BoxGeometry(0.42, 0.32, 0.4), extras, 0, 1.13, 0, COAL);
  mesh(new THREE.BoxGeometry(0.76, 0.1, 0.64), extras, 0, 0.4, 0, 0xd6303a);  // scarf
  mesh(new THREE.BoxGeometry(0.14, 0.32, 0.05), extras, 0.22, 0.22, 0.32, 0xd6303a).rotation.z = 0.15;  // scarf tail
  mesh(new THREE.ConeGeometry(0.05, 0.25, 4), extras, 0, 0.66, 0.38, 0xff8c1a).rotation.x = Math.PI / 2;  // carrot nose
  const dot = new THREE.BoxGeometry(0.07, 0.07, 0.04);
  for (const [x, y] of [[-0.12, 0.78], [0.12, 0.78], [-0.15, 0.56], [-0.05, 0.52], [0.05, 0.52], [0.15, 0.56], [0, 0.18], [0, 0.03], [0, -0.12], [0, -0.45]])
    mesh(dot, extras, x, y, y > 0.3 ? 0.28 : y > -0.25 ? 0.32 : 0.37, COAL);
  const arm = new THREE.CylinderGeometry(0.025, 0.025, 0.55, 4);
  const arms = [-1, 1].map((side) => { const m = mesh(arm, extras, side * 0.55, 0.15, 0, 0x7a4b23); m.rotation.z = side; return m; });
  const snow = many(new THREE.OctahedronGeometry(0.018, 0), 110, ambient, WHITE, (i, t, d) => {
    d.position.set((hash(i) - 0.5) * 2.8, 1.5 - ((t * 0.18 + hash(i + 5) * 3) % 3), (hash(i + 9) - 0.5) * 1.8);
  });
  const ground = mesh(new THREE.CylinderGeometry(1.2, 1.3, 0.06, 16), group, 0, -1.0);
  return {
    object: group,
    ambient,
    update(t) {
      ground.scale.setScalar(grown(t / 0.5));
      parts.forEach((p, i) => p.scale.setScalar(grown((t - 0.3 - i * 0.5) / 0.5)));
      extras.scale.setScalar(grown((t - 1.8) / 0.5));
      arms.forEach((a, i) => { a.rotation.z = (i ? 1 : -1) * (1 + 0.25 * Math.sin(t * 3 + i)); });
      body.position.y = Math.abs(Math.sin(t * 2.2)) * 0.06 * grown((t - 2.4) / 0.5);  // a little hop
      snow(t);
    },
  };
}
