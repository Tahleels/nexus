/**
 * Nexus Flow - High-Performance 3D Organic Particle Background
 * Designed specifically for the Nexus Web Platform.
 * 
 * Features:
 *   - Zero CPU/GPU load when tab is hidden (Page Visibility API)
 *   - Auto Dark/Light theme color adaptation (MutationObserver)
 *   - Non-blocking asynchronous initialization
 *   - Respects prefers-reduced-motion
 */

(function () {
  'use strict';

  // Config & Theme Palettes
  // Colors mirror the CSS custom properties in static/css/theme.css
  // (--bg-app / --accent / --accent-hover / --accent-light) so the
  // particles always match the active app theme, not an arbitrary palette.
  const PALETTES = {
    dark: {
      colorBase: '#0d0a0c',  // --bg-app (dark)
      colorOne: '#C74759',   // --accent (dark)
      colorTwo: '#D8637A',   // --accent-hover (dark)
      colorThree: '#ffd5e5', // Glowing Soft Pink (brand-hue highlight)
      opacity: 0.75
    },
    light: {
      colorBase: '#f7f5f6',  // --bg-app (light)
      colorOne: '#A22E57',   // --accent (light) — Phantom Red
      colorTwo: '#6B2038',   // --accent-hover (light)
      colorThree: '#F3DCE0', // --accent-light
      opacity: 0.45
    }
  };

  let canvas, renderer, scene, camera, material, mesh, animationFrameId = null;
  let isRunning = false;
  let mouse = { x: 0, y: 0, targetX: 0, targetY: 0 };
  let clock;

  function getActiveTheme() {
    const theme = document.documentElement.getAttribute('data-theme') || 
                  document.documentElement.getAttribute('data-bs-theme') || 'light';
    return theme === 'dark' ? 'dark' : 'light';
  }

  function initFlow() {
    canvas = document.getElementById('nexus-flow-canvas');
    if (!canvas || typeof THREE === 'undefined') return;

    // Check accessibility preference
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      canvas.style.display = 'none';
      return;
    }

    // 1. Scene & Camera Setup
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 100);
    camera.position.z = 5;

    // 2. WebGL Renderer with performance caps
    renderer = new THREE.WebGLRenderer({
      canvas: canvas,
      alpha: true,
      antialias: false, // Disabled antialiasing for maximum FPS
      powerPreference: 'low-power'
    });

    const pixelRatio = Math.min(window.devicePixelRatio || 1, 1.5);
    renderer.setPixelRatio(pixelRatio);
    renderer.setSize(window.innerWidth, window.innerHeight, false);

    clock = new THREE.Clock();

    // 3. Create Particle Grid
    const countX = 75;
    const countY = 40;
    const count = countX * countY;

    const geometry = new THREE.PlaneGeometry(1, 1);
    const activePalette = PALETTES[getActiveTheme()];

    const uniforms = {
      uTime: { value: 0 },
      uMouse: { value: new THREE.Vector2(0, 0) },
      uColorBase: { value: new THREE.Color(activePalette.colorBase) },
      uColorOne: { value: new THREE.Color(activePalette.colorOne) },
      uColorTwo: { value: new THREE.Color(activePalette.colorTwo) },
      uColorThree: { value: new THREE.Color(activePalette.colorThree) }
    };

    material = new THREE.ShaderMaterial({
      uniforms: uniforms,
      transparent: true,
      depthWrite: false,
      vertexShader: `
        uniform float uTime;
        uniform vec2 uMouse;
        varying vec2 vUv;
        varying float vSize;
        varying vec2 vPos;
        attribute vec3 aOffset;

        float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
        float noise(vec2 p) {
          vec2 i = floor(p); vec2 f = fract(p); f = f * f * (3.0 - 2.0 * f);
          return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y);
        }

        void main() {
          vUv = uv;
          vec3 pos = aOffset;
          float t = uTime * 0.15;

          // Organic ambient sway
          pos.x += (sin(t + pos.y * 0.5) + sin(t * 0.5 + pos.y * 2.0)) * 0.2;
          pos.y += (cos(t + pos.x * 0.5) + cos(t * 0.5 + pos.x * 2.0)) * 0.2;

          vec2 relToMouse = pos.xy - uMouse;
          float dist = length(relToMouse / vec2(1.3, 1.0));
          float breathCycle = sin(uTime * 0.8);
          float radius = 2.2 + breathCycle * 0.4 + noise(normalize(relToMouse + vec2(0.0001)) * 2.0 + vec2(0.0, uTime * 0.1)) * 0.6;
          float rimInfluence = smoothstep(1.6, 0.0, abs(dist - radius));

          pos.xy += normalize(relToMouse + vec2(0.0001)) * (breathCycle * 0.2 + 0.2) * rimInfluence;
          pos.z += rimInfluence * 0.25 * sin(uTime);

          float scale = 0.02 + (rimInfluence * 0.048);
          vec3 transformed = position;
          transformed.x *= scale * 1.2;
          transformed.y *= scale * 0.7;

          vec2 dir = relToMouse / max(length(relToMouse), 0.0001);
          transformed.xy = mat2(dir.x, dir.y, -dir.y, dir.x) * transformed.xy;

          vSize = rimInfluence;
          vPos = pos.xy;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(pos + transformed, 1.0);
        }
      `,
      fragmentShader: `
        uniform float uTime;
        uniform vec3 uColorBase;
        uniform vec3 uColorOne;
        uniform vec3 uColorTwo;
        uniform vec3 uColorThree;
        varying vec2 vUv;
        varying float vSize;
        varying vec2 vPos;

        void main() {
          vec2 pos = abs(vUv - vec2(0.5)) * 2.0;
          float alpha = 1.0 - smoothstep(0.8, 1.0, pow(pow(pos.x, 2.6) + pow(pos.y, 2.6), 1.0/2.6));
          if (alpha < 0.01) discard;

          float t = uTime * 1.2;
          float p1 = sin(vPos.x * 0.8 + t);
          float p2 = sin(vPos.y * 0.8 + t * 0.8 + p1);

          vec3 activeColor = mix(uColorOne, uColorTwo, p1 * 0.5 + 0.5);
          activeColor = mix(activeColor, uColorThree, p2 * 0.5 + 0.5);

          vec3 finalColor = mix(uColorBase, activeColor, smoothstep(0.1, 0.8, vSize));
          gl_FragColor = vec4(finalColor, alpha * mix(0.35, 0.9, vSize));
        }
      `
    });

    const offsets = new Float32Array(count * 3);
    let i = 0;
    for (let y = 0; y < countY; y++) {
      for (let x = 0; x < countX; x++) {
        offsets[i * 3] = (x / (countX - 1) - 0.5) * 38 + (Math.random() - 0.5) * 0.2;
        offsets[i * 3 + 1] = (y / (countY - 1) - 0.5) * 22 + (Math.random() - 0.5) * 0.2;
        offsets[i * 3 + 2] = 0;
        i++;
      }
    }
    geometry.setAttribute('aOffset', new THREE.InstancedBufferAttribute(offsets, 3));

    mesh = new THREE.InstancedMesh(geometry, material, count);
    scene.add(mesh);

    // Event Listeners
    window.addEventListener('resize', onWindowResize, false);
    window.addEventListener('pointermove', onPointerMove, { passive: true });

    // Page Visibility Listener — Pauses loop when tab is hidden
    document.addEventListener('visibilitychange', onVisibilityChange, false);

    // Theme MutationObserver
    observeThemeChanges();

    startLoop();
  }

  function updatePalette(theme) {
    if (!material) return;
    const palette = PALETTES[theme] || PALETTES.dark;
    material.uniforms.uColorBase.value.set(palette.colorBase);
    material.uniforms.uColorOne.value.set(palette.colorOne);
    material.uniforms.uColorTwo.value.set(palette.colorTwo);
    material.uniforms.uColorThree.value.set(palette.colorThree);
  }

  function observeThemeChanges() {
    const observer = new MutationObserver(() => {
      updatePalette(getActiveTheme());
    });
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme', 'data-bs-theme']
    });
  }

  function onPointerMove(e) {
    mouse.targetX = (e.clientX / window.innerWidth) * 2 - 1;
    mouse.targetY = -(e.clientY / window.innerHeight) * 2 + 1;
  }

  function onWindowResize() {
    if (!camera || !renderer) return;
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight, false);
  }

  function onVisibilityChange() {
    if (document.hidden) {
      stopLoop();
    } else {
      startLoop();
    }
  }

  function startLoop() {
    if (isRunning) return;
    isRunning = true;
    clock.start();
    animate();
  }

  function stopLoop() {
    if (!isRunning) return;
    isRunning = false;
    if (animationFrameId) {
      cancelAnimationFrame(animationFrameId);
      animationFrameId = null;
    }
    clock.stop();
  }

  function animate() {
    if (!isRunning) return;
    animationFrameId = requestAnimationFrame(animate);

    const elapsedTime = clock.getElapsedTime();
    material.uniforms.uTime.value = elapsedTime;

    // Smooth mouse damping
    mouse.x += (mouse.targetX - mouse.x) * 0.025;
    mouse.y += (mouse.targetY - mouse.y) * 0.025;

    const vWidth = camera.aspect * 5;
    const vHeight = 5;
    material.uniforms.uMouse.value.set((mouse.x * vWidth) / 2, (mouse.y * vHeight) / 2);

    renderer.render(scene, camera);
  }

  // Initialize after DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFlow);
  } else {
    initFlow();
  }

  // Expose global control API for optional user toggle
  window.nexusFlow = {
    pause: stopLoop,
    resume: startLoop,
    setTheme: updatePalette
  };

})();
