const toast = (message) => {
  const node = document.querySelector('.toast');
  if (!node) return;
  node.textContent = message;
  node.classList.add('show');
  window.clearTimeout(window.__toastTimer);
  window.__toastTimer = window.setTimeout(() => node.classList.remove('show'), 2200);
};

document.querySelectorAll('.nav button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelector('.nav button.active')?.classList.remove('active');
    button.classList.add('active');
    const label = button.textContent.trim();
    if (label !== '운영 현황' && label !== '종목 시세') toast(`${label} 화면 시안`);
  });
});

document.querySelectorAll('.switch').forEach((button) => {
  button.addEventListener('click', () => {
    button.classList.toggle('on');
    const active = button.classList.contains('on');
    button.setAttribute('aria-checked', String(active));
    toast(`${button.dataset.name}: ${active ? '활성' : '중지'} (시안)`);
  });
});

document.querySelectorAll('.segmented button').forEach((button) => {
  button.addEventListener('click', () => {
    button.parentElement.querySelector('.active')?.classList.remove('active');
    button.classList.add('active');
  });
});

document.querySelectorAll('.order-side button').forEach((button) => {
  button.addEventListener('click', () => {
    button.parentElement.querySelector('.active')?.classList.remove('active');
    button.classList.add('active');
    const submit = document.querySelector('.order-submit');
    if (submit) {
      const buying = button.classList.contains('buy');
      submit.textContent = buying ? '매수 주문 검토' : '매도 주문 검토';
      submit.classList.toggle('danger', !buying);
    }
  });
});

document.querySelectorAll('.order-submit').forEach((button) => {
  button.addEventListener('click', () => {
    const side = document.querySelector('.order-side .active')?.textContent.trim() || '주문';
    toast(`${side} 확인 단계 (시안 · 실제 주문 미전송)`);
  });
});

document.querySelectorAll('.tabbar button').forEach((button) => {
  button.addEventListener('click', () => {
    button.parentElement.querySelector('.active')?.classList.remove('active');
    button.classList.add('active');
    toast(`${button.textContent.trim()} 목록 시안`);
  });
});

const chart = document.querySelector('#price-chart');
if (chart) {
  const layer = chart.querySelector('#drawing-layer');
  const surface = chart.querySelector('#drawing-surface');
  const hint = document.querySelector('#drawing-hint');
  const storageKey = 'inazina-hts-mock-drawings-v1';
  const ns = 'http://www.w3.org/2000/svg';
  let drawings = [];
  let selectedId = null;
  let activeTool = 'select';
  let gesture = null;

  try {
    drawings = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (!Array.isArray(drawings)) drawings = [];
  } catch {
    drawings = [];
  }

  const save = () => {
    try { localStorage.setItem(storageKey, JSON.stringify(drawings)); } catch { /* Local preview storage may be blocked. */ }
  };
  const setHint = (message) => { if (hint) hint.textContent = message; };
  const pointAt = (event) => {
    const rect = chart.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(650, (event.clientX - rect.left) / rect.width * 650)),
      y: Math.max(0, Math.min(250, (event.clientY - rect.top) / rect.height * 250)),
    };
  };
  const toChartPoint = (point) => ({x: point.x * 650, y: point.y * 250});
  const distanceToSegment = (point, a, b) => {
    const dx = b.x - a.x, dy = b.y - a.y;
    const length = dx * dx + dy * dy;
    const t = length ? Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / length)) : 0;
    return Math.hypot(point.x - (a.x + t * dx), point.y - (a.y + t * dy));
  };
  const newSvgElement = (name, attrs) => {
    const element = document.createElementNS(ns, name);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
    return element;
  };
  const render = () => {
    layer.replaceChildren();
    drawings.forEach((drawing) => {
      const a = toChartPoint({x:drawing.x1,y:drawing.y1});
      const b = toChartPoint({x:drawing.x2,y:drawing.y2});
      const line = newSvgElement('line', {
        x1:a.x, y1:a.y, x2:b.x, y2:b.y,
        class:`drawing-line${drawing.id === selectedId ? ' selected' : ''}`,
      });
      layer.append(line);
      if (drawing.id === selectedId) {
        layer.append(newSvgElement('circle', {cx:a.x,cy:a.y,r:3.5,class:'drawing-handle'}));
        layer.append(newSvgElement('circle', {cx:b.x,cy:b.y,r:3.5,class:'drawing-handle'}));
      }
    });
    if (gesture?.preview) {
      const a = toChartPoint(gesture.start), b = toChartPoint(gesture.current);
      layer.append(newSvgElement('line', {x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:'drawing-line preview'}));
    }
  };
  const addDrawing = (type, start, end) => {
    drawings.push({id:`${Date.now()}-${Math.random().toString(16).slice(2)}`,type,
      x1:start.x/650,y1:start.y/250,x2:end.x/650,y2:end.y/250});
    selectedId = drawings[drawings.length-1].id;
    save(); render();
  };
  const removeSelected = () => {
    if (!selectedId) return;
    drawings = drawings.filter((drawing) => drawing.id !== selectedId);
    selectedId = null; save(); render();
    setHint('선택한 선을 삭제했습니다');
  };
  const setTool = (tool) => {
    activeTool = tool; chart.dataset.tool = tool;
    document.querySelectorAll('.drawing-tool[data-tool]').forEach((button) => {
      const active = button.dataset.tool === tool;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    setHint(tool === 'trend' ? '차트를 드래그해 추세선을 그리세요' :
      tool === 'horizontal' ? '수평선을 놓을 가격 위치를 클릭하세요' :
      '선을 눌러 선택하고 드래그해 옮길 수 있습니다');
  };

  surface.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    const point = pointAt(event);
    if (activeTool === 'horizontal') {
      addDrawing('horizontal', {x:0,y:point.y}, {x:650,y:point.y});
      return;
    }
    if (activeTool === 'trend') {
      gesture = {kind:'draw',type:'trend',start:point,current:point,preview:true};
      surface.setPointerCapture(event.pointerId); render();
      return;
    }
    const chartPoint = {x:point.x,y:point.y};
    const nearest = drawings.map((drawing) => ({drawing, distance:distanceToSegment(chartPoint,
      {x:drawing.x1*650,y:drawing.y1*250},{x:drawing.x2*650,y:drawing.y2*250})}))
      .sort((a,b) => a.distance-b.distance)[0];
    selectedId = nearest && nearest.distance < 10 ? nearest.drawing.id : null;
    if (selectedId) {
      gesture = {kind:'move',start:point,original:{...drawings.find((drawing)=>drawing.id===selectedId)}};
      surface.setPointerCapture(event.pointerId);
    }
    render();
  });

  surface.addEventListener('pointermove', (event) => {
    if (!gesture) return;
    const point = pointAt(event);
    if (gesture.kind === 'draw') {
      gesture.current = point; render(); return;
    }
    const dx = (point.x - gesture.start.x) / 650;
    const dy = (point.y - gesture.start.y) / 250;
    const original = gesture.original;
    const drawing = drawings.find((item) => item.id === selectedId);
    if (!drawing) return;
    drawing.x1 = Math.max(0,Math.min(1,original.x1+dx));
    drawing.x2 = Math.max(0,Math.min(1,original.x2+dx));
    drawing.y1 = Math.max(0,Math.min(1,original.y1+dy));
    drawing.y2 = Math.max(0,Math.min(1,original.y2+dy));
    render();
  });

  const finishGesture = (event) => {
    if (!gesture) return;
    if (gesture.kind === 'draw') {
      const end = pointAt(event);
      if (Math.hypot(end.x-gesture.start.x,end.y-gesture.start.y) > 5) {
        const start = {...gesture.start};
        gesture = null; addDrawing('trend',start,end); return;
      }
    } else if (gesture.kind === 'move') save();
    gesture = null; render();
  };
  surface.addEventListener('pointerup', finishGesture);
  surface.addEventListener('pointercancel', finishGesture);

  document.querySelectorAll('.drawing-tool[data-tool]').forEach((button) =>
    button.addEventListener('click', () => setTool(button.dataset.tool)));
  document.querySelector('[data-action="undo"]')?.addEventListener('click', () => {
    if (!drawings.length) return;
    drawings.pop(); selectedId = null; save(); render(); setHint('마지막 선을 실행 취소했습니다');
  });
  document.querySelector('[data-action="clear"]')?.addEventListener('click', () => {
    if (!drawings.length) return;
    if (!window.confirm('차트에 그린 선을 모두 지울까요?')) return;
    drawings = []; selectedId = null; save(); render(); setHint('그린 선을 모두 지웠습니다');
  });
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { gesture = null; setTool('select'); render(); }
    if ((event.key === 'Delete' || event.key === 'Backspace') && selectedId && !event.target.matches('input,textarea')) {
      event.preventDefault(); removeSelected();
    }
  });
  setTool('select'); render();
}
