/* HairDaze background animation v3 — bold gradient blobs + bright flow (forced ON) */
(() => {
  try {
    if (window.HD_DISABLE_BG) return;

    const canvas = document.getElementById('bgCanvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d', { alpha: true });

    const CONFIG = {
      respectReduceMotion: false, // force ON (so it doesn't stop after 1 frame)
      intensity: 1.8,
      blobCount: 6,
      blobAlpha: 0.65,
      tealBias: 0.55,
      showFlow: true,
    };

    const prefersReduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let running = CONFIG.respectReduceMotion ? !prefersReduce : true;

    function resize() {
      const dpr = Math.min(devicePixelRatio || 1, 2);
      const w = innerWidth, h = innerHeight;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      canvas.style.width = w + 'px';
      canvas.style.height = h + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize(); addEventListener('resize', resize);

    // Blobs
    const huePeach = 18;
    const hueTealMax = Math.round(18 + 160 * (0.35 + 0.65 * CONFIG.tealBias));
    const blobs = Array.from({ length: CONFIG.blobCount }, () => ({
      r: Math.min(innerWidth, innerHeight) * (0.32 + Math.random() * 0.18),
      hue: Math.random() < 0.5 ? huePeach + Math.random()*15 : hueTealMax - Math.random()*15,
      speedX: (0.12 + Math.random() * 0.18) * CONFIG.intensity,
      speedY: (0.10 + Math.random() * 0.16) * CONFIG.intensity,
      phaseX: Math.random()*Math.PI*2,
      phaseY: Math.random()*Math.PI*2
    }));

    // Flow lines
    const parts = (() => {
      if (!CONFIG.showFlow) return [];
      const N = Math.round(140 * CONFIG.intensity);
      return Array.from({ length: N }, () => ({
        x: Math.random() * innerWidth,
        y: Math.random() * innerHeight,
        v: (0.7 + Math.random() * 1.3) * CONFIG.intensity,
        w: (1.0 + Math.random() * 1.8) * (0.9 + 0.3 * CONFIG.intensity),
        life: 70 + Math.random() * 140,
        hue: huePeach + Math.random() * (hueTealMax - huePeach)
      }));
    })();

    let t = 0, last = performance.now();
    function field(x, y) {
      const s = 0.0014 * (0.9 + 0.25 * CONFIG.intensity);
      return Math.sin((x + y) * s + t * 0.85) + Math.cos((x - y) * s * 1.05 - t * 0.45);
    }

    function draw(now) {
      if (!running) return;
      const dt = Math.min(50, now - last); last = now; t += dt / (6000 / CONFIG.intensity);

      // fade old frame
      ctx.globalCompositeOperation = 'source-over';
      ctx.fillStyle = 'rgba(255,255,255,0.06)';
      ctx.fillRect(0, 0, innerWidth, innerHeight);

      // blobs
      ctx.globalCompositeOperation = 'screen';
      blobs.forEach(b => {
        const time = t * 8;
        const cx = (innerWidth*0.5) + Math.cos(time*b.speedX + b.phaseX) * (innerWidth*0.28);
        const cy = (innerHeight*0.5) + Math.sin(time*b.speedY + b.phaseY) * (innerHeight*0.28);
        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, b.r);
        g.addColorStop(0, `hsla(${b.hue}, 90%, 65%, ${CONFIG.blobAlpha})`);
        g.addColorStop(1, `hsla(${b.hue}, 90%, 65%, 0)`);
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(cx, cy, b.r, 0, Math.PI*2); ctx.fill();
      });

      // flow
      if (parts.length) {
        ctx.globalCompositeOperation = 'lighter';
        for (const p of parts) {
          const a = field(p.x, p.y), vx = Math.cos(a)*p.v, vy = Math.sin(a)*p.v;
          ctx.strokeStyle = `hsla(${p.hue}, 85%, 60%, 0.22)`;
          ctx.lineWidth = p.w;
          ctx.beginPath(); ctx.moveTo(p.x, p.y);
          p.x += vx * dt * 0.09; p.y += vy * dt * 0.09;
          ctx.lineTo(p.x, p.y); ctx.stroke();
          p.hue += 0.22; if (p.hue > hueTealMax) p.hue = huePeach;
          if (--p.life < 0 || p.x < -160 || p.x > innerWidth+160 || p.y < -160 || p.y > innerHeight+160) {
            p.x = Math.random()*innerWidth; p.y = Math.random()*innerHeight; p.life = 70 + Math.random()*140;
          }
        }
      }
      requestAnimationFrame(draw);
    }

    document.addEventListener('visibilitychange', () => {
      running = !document.hidden || !CONFIG.respectReduceMotion;
      if (running) { last = performance.now(); requestAnimationFrame(draw); }
    });

    if (running) requestAnimationFrame(draw);
  } catch (e) {
    console.warn('HairDaze background disabled:', e);
  }
})();
