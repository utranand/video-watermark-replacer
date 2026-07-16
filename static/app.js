'use strict';

(function () {
  var ELEMENT_IDS = [
    'video-select', 'video-upload', 'image-select', 'image-upload',
    'video-browse-btn', 'image-browse-btn',
    'preview-canvas', 'time-slider', 'time-label',
    'detect-btn', 'detect-info', 'manual-indicator',
    'box-x', 'box-y', 'box-w', 'box-h',
    'sizing-mode-scale', 'sizing-mode-fit',
    'scale-input', 'output-name-input', 'output-dir-input', 'output-dir-browse-btn',
    'auto-detect-checkbox',
    'process-btn', 'busy-indicator',
    'result-panel', 'result-video', 'download-link',
    'status-area',
    'browse-modal', 'browse-modal-title', 'browse-modal-close-btn',
    'browse-modal-path', 'browse-modal-list',
    'browse-modal-select-btn', 'browse-modal-cancel-btn'
  ];

  var HANDLE_SIZE = 12;
  var MAX_DISPLAY_W = 800;

  var els = {};

  var state = {
    videos: [],
    images: [],
    selectedVideo: '',
    selectedImage: '',
    box: null,            // {x,y,w,h} in SOURCE pixel coordinates, or null
    manualOverride: false,
    sourceW: 0,
    sourceH: 0,
    displayW: 0,
    displayH: 0,
    scale: 1,              // sourceW / displayW
    currentFrameObjectUrl: null,
    frameImg: null,
    drag: null,
    sizingMode: 'scale',   // 'scale' | 'fit'
    browse: { kind: null, dir: '', parent: null, onSelect: null }
  };

  function init() {
    ELEMENT_IDS.forEach(function (id) {
      els[id] = document.getElementById(id);
    });

    els['video-select'].addEventListener('change', function (e) {
      selectVideo(e.target.value);
    });
    els['image-select'].addEventListener('change', function (e) {
      state.selectedImage = e.target.value;
    });
    els['video-upload'].addEventListener('change', onVideoUpload);
    els['image-upload'].addEventListener('change', onImageUpload);

    els['time-slider'].addEventListener('input', onTimeInput);
    els['time-slider'].addEventListener('change', onTimeChange);

    els['detect-btn'].addEventListener('click', onDetectClick);
    els['process-btn'].addEventListener('click', onProcessClick);

    ['box-x', 'box-y', 'box-w', 'box-h'].forEach(function (id) {
      els[id].addEventListener('change', onBoxInputChange);
    });

    els['sizing-mode-scale'].addEventListener('change', onSizingModeChange);
    els['sizing-mode-fit'].addEventListener('change', onSizingModeChange);
    onSizingModeChange();

    els['video-browse-btn'].addEventListener('click', onVideoBrowseClick);
    els['image-browse-btn'].addEventListener('click', onImageBrowseClick);
    els['output-dir-browse-btn'].addEventListener('click', onOutputDirBrowseClick);
    els['browse-modal-close-btn'].addEventListener('click', closeBrowseModal);
    els['browse-modal-cancel-btn'].addEventListener('click', closeBrowseModal);
    els['browse-modal-select-btn'].addEventListener('click', onBrowseSelectFolderClick);
    document.addEventListener('keydown', onBrowseModalKeydown);

    var canvas = els['preview-canvas'];
    canvas.addEventListener('mousedown', onCanvasMouseDown);
    window.addEventListener('mousemove', onCanvasMouseMove);
    window.addEventListener('mouseup', onCanvasMouseUp);

    loadResources();
  }

  // ---------------------------------------------------------------------
  // status area
  // ---------------------------------------------------------------------
  function setStatus(message, isError) {
    var area = els['status-area'];
    area.textContent = message || '';
    area.classList.toggle('error', !!isError);
  }

  // ---------------------------------------------------------------------
  // resources
  // ---------------------------------------------------------------------
  async function loadResources() {
    try {
      var res = await fetch('/api/resources');
      var data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || 'failed to load resources');
      }
      state.videos = data.videos || [];
      state.images = data.images || [];

      populateSelect(els['video-select'], state.videos, '-- select video --');
      populateSelect(els['image-select'], state.images, '-- select image --');

      var defaultImage = state.images.find(function (img) {
        return img.name === 'paper-sticker.png';
      });
      if (defaultImage) {
        els['image-select'].value = defaultImage.path;
        state.selectedImage = defaultImage.path;
      }
      setStatus('');
    } catch (err) {
      setStatus('Failed to load resources: ' + err.message, true);
    }
  }

  function populateSelect(select, items, placeholder) {
    select.innerHTML = '';
    var placeholderOpt = document.createElement('option');
    placeholderOpt.value = '';
    placeholderOpt.textContent = placeholder;
    select.appendChild(placeholderOpt);
    items.forEach(function (item) {
      var opt = document.createElement('option');
      opt.value = item.path;
      opt.textContent = item.name;
      select.appendChild(opt);
    });
  }

  // ---------------------------------------------------------------------
  // video / image selection + upload
  // ---------------------------------------------------------------------
  function selectVideo(path) {
    state.selectedVideo = path;
    state.box = null;
    state.manualOverride = false;
    updateManualIndicator();
    updateBoxInputs();
    els['detect-info'].textContent = '';
    delete els['detect-info'].dataset.method;
    delete els['detect-info'].dataset.confidence;
    els['time-slider'].value = '0';
    els['time-label'].textContent = '0.0s';

    if (path) {
      loadFrame(path, 0);
    } else {
      state.frameImg = null;
      clearCanvas();
    }
  }

  async function onVideoUpload(e) {
    var file = e.target.files[0];
    if (!file) return;
    try {
      setStatus('Uploading video…');
      var path = await uploadFile(file);
      addOptionAndSelect(els['video-select'], file.name, path);
      state.videos.push({ name: file.name, path: path });
      selectVideo(path);
      setStatus('');
    } catch (err) {
      setStatus('Video upload failed: ' + err.message, true);
    } finally {
      e.target.value = '';
    }
  }

  async function onImageUpload(e) {
    var file = e.target.files[0];
    if (!file) return;
    try {
      setStatus('Uploading image…');
      var path = await uploadFile(file);
      addOptionAndSelect(els['image-select'], file.name, path);
      state.images.push({ name: file.name, path: path });
      els['image-select'].value = path;
      state.selectedImage = path;
      setStatus('');
    } catch (err) {
      setStatus('Image upload failed: ' + err.message, true);
    } finally {
      e.target.value = '';
    }
  }

  function addOptionAndSelect(select, name, path) {
    var opt = document.createElement('option');
    opt.value = path;
    opt.textContent = name;
    select.appendChild(opt);
    select.value = path;
  }

  async function uploadFile(file) {
    var fd = new FormData();
    fd.append('file', file);
    var res = await fetch('/api/upload', { method: 'POST', body: fd });
    var data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'upload failed');
    }
    return data.path;
  }

  // ---------------------------------------------------------------------
  // frame preview
  // ---------------------------------------------------------------------
  function onTimeInput(e) {
    els['time-label'].textContent = Number(e.target.value).toFixed(1) + 's';
  }

  function onTimeChange(e) {
    if (!state.selectedVideo) return;
    loadFrame(state.selectedVideo, Number(e.target.value));
  }

  async function loadFrame(video, t) {
    try {
      setStatus('Loading frame…');
      var url = '/api/frame?video=' + encodeURIComponent(video) + '&t=' + encodeURIComponent(t);
      var res = await fetch(url);
      if (!res.ok) {
        var msg = 'failed to load frame';
        try {
          var data = await res.json();
          msg = data.error || msg;
        } catch (parseErr) {
          // response wasn't JSON; keep default message
        }
        throw new Error(msg);
      }
      var blob = await res.blob();
      await drawFrameBlob(blob);
      setStatus('');
    } catch (err) {
      setStatus('Failed to load frame: ' + err.message, true);
    }
  }

  function drawFrameBlob(blob) {
    return new Promise(function (resolve, reject) {
      var objUrl = URL.createObjectURL(blob);
      var img = new Image();
      img.onload = function () {
        if (state.currentFrameObjectUrl) {
          URL.revokeObjectURL(state.currentFrameObjectUrl);
        }
        state.currentFrameObjectUrl = objUrl;
        state.frameImg = img;
        setupCanvasForFrame(img);
        drawFrame();
        resolve();
      };
      img.onerror = function () {
        URL.revokeObjectURL(objUrl);
        reject(new Error('could not decode frame image'));
      };
      img.src = objUrl;
    });
  }

  function setupCanvasForFrame(img) {
    state.sourceW = img.naturalWidth;
    state.sourceH = img.naturalHeight;
    var displayW = Math.min(state.sourceW, MAX_DISPLAY_W) || 1;
    var displayH = Math.max(1, Math.round(state.sourceH * (displayW / state.sourceW)));
    state.displayW = displayW;
    state.displayH = displayH;
    state.scale = state.sourceW / displayW;

    var canvas = els['preview-canvas'];
    canvas.width = displayW;
    canvas.height = displayH;
  }

  function clearCanvas() {
    var canvas = els['preview-canvas'];
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function drawFrame() {
    var canvas = els['preview-canvas'];
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (state.frameImg) {
      ctx.drawImage(state.frameImg, 0, 0, canvas.width, canvas.height);
    }
    if (state.box) {
      drawBox(ctx);
    }
  }

  function drawBox(ctx) {
    var d = sourceBoxToDisplay(state.box);

    ctx.save();
    ctx.strokeStyle = '#e63946';
    ctx.lineWidth = 2;
    ctx.strokeRect(d.x, d.y, d.w, d.h);

    ctx.fillStyle = '#e63946';
    boxCorners(d).forEach(function (c) {
      ctx.fillRect(c.x - HANDLE_SIZE / 2, c.y - HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE);
    });

    var label = boxLabel();
    if (label) {
      ctx.font = '12px system-ui, sans-serif';
      var textW = ctx.measureText(label).width + 8;
      var labelY = d.y > 18 ? d.y - 18 : d.y + d.h + 2;
      ctx.fillStyle = 'rgba(230, 57, 70, 0.9)';
      ctx.fillRect(d.x, labelY, textW, 16);
      ctx.fillStyle = '#fff';
      ctx.fillText(label, d.x + 4, labelY + 12);
    }
    ctx.restore();
  }

  function boxLabel() {
    var info = els['detect-info'];
    if (info.dataset.confidence) {
      var pct = (Number(info.dataset.confidence) * 100).toFixed(0);
      return (info.dataset.method || 'auto') + ' ' + pct + '%';
    }
    return '';
  }

  function sourceBoxToDisplay(box) {
    return {
      x: box.x / state.scale,
      y: box.y / state.scale,
      w: box.w / state.scale,
      h: box.h / state.scale
    };
  }

  function boxCorners(d) {
    return [
      { name: 'tl', x: d.x, y: d.y },
      { name: 'tr', x: d.x + d.w, y: d.y },
      { name: 'bl', x: d.x, y: d.y + d.h },
      { name: 'br', x: d.x + d.w, y: d.y + d.h }
    ];
  }

  // ---------------------------------------------------------------------
  // box state (single source of truth: state.box, in SOURCE pixel coords)
  // ---------------------------------------------------------------------
  function setBox(box, fromUser) {
    state.box = clampBox(box);
    if (fromUser) {
      state.manualOverride = true;
    }
    updateManualIndicator();
    updateBoxInputs();
    drawFrame();
  }

  function clampBox(box) {
    var w = Math.max(1, box.w);
    var h = Math.max(1, box.h);
    var maxX = Math.max(0, (state.sourceW || w) - w);
    var maxY = Math.max(0, (state.sourceH || h) - h);
    var x = Math.min(Math.max(0, box.x), maxX);
    var y = Math.min(Math.max(0, box.y), maxY);
    return {
      x: Math.round(x),
      y: Math.round(y),
      w: Math.round(w),
      h: Math.round(h)
    };
  }

  function updateManualIndicator() {
    els['manual-indicator'].classList.toggle('hidden', !state.manualOverride);
    els['auto-detect-checkbox'].checked = !state.manualOverride;
  }

  function updateBoxInputs() {
    if (!state.box) {
      ['box-x', 'box-y', 'box-w', 'box-h'].forEach(function (id) {
        els[id].value = '';
      });
      return;
    }
    els['box-x'].value = state.box.x;
    els['box-y'].value = state.box.y;
    els['box-w'].value = state.box.w;
    els['box-h'].value = state.box.h;
  }

  function onBoxInputChange() {
    var x = Number(els['box-x'].value);
    var y = Number(els['box-y'].value);
    var w = Number(els['box-w'].value);
    var h = Number(els['box-h'].value);
    if (!isFinite(x)) x = state.box ? state.box.x : 0;
    if (!isFinite(y)) y = state.box ? state.box.y : 0;
    if (!isFinite(w) || w <= 0) w = state.box ? state.box.w : 1;
    if (!isFinite(h) || h <= 0) h = state.box ? state.box.h : 1;
    setBox({ x: x, y: y, w: w, h: h }, true);
  }

  // ---------------------------------------------------------------------
  // sizing mode (scale vs. fit-to-box)
  // ---------------------------------------------------------------------
  function onSizingModeChange() {
    state.sizingMode = els['sizing-mode-fit'].checked ? 'fit' : 'scale';
    els['scale-input'].disabled = state.sizingMode === 'fit';
  }

  // ---------------------------------------------------------------------
  // auto-detect
  // ---------------------------------------------------------------------
  async function onDetectClick() {
    if (!state.selectedVideo) {
      setStatus('Select a video first', true);
      return;
    }
    els['detect-btn'].disabled = true;
    try {
      setStatus('Detecting watermark…');
      var res = await fetch('/api/detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video: state.selectedVideo })
      });
      var data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || 'detect failed');
      }
      var box = data.box || {};
      els['detect-info'].dataset.method = box.method || 'auto';
      els['detect-info'].dataset.confidence = box.confidence != null ? String(box.confidence) : '';
      els['detect-info'].textContent =
        (box.method || 'auto') + ' — confidence ' +
        (Number(box.confidence || 0) * 100).toFixed(0) + '%';

      state.manualOverride = false;
      setBox({ x: box.x, y: box.y, w: box.w, h: box.h }, false);
      setStatus('');
    } catch (err) {
      setStatus('Detect failed: ' + err.message, true);
    } finally {
      els['detect-btn'].disabled = false;
    }
  }

  // ---------------------------------------------------------------------
  // canvas drag / resize
  // ---------------------------------------------------------------------
  function canvasPoint(e) {
    var rect = els['preview-canvas'].getBoundingClientRect();
    var scaleX = els['preview-canvas'].width / rect.width;
    var scaleY = els['preview-canvas'].height / rect.height;
    return {
      x: (e.clientX - rect.left) * scaleX,
      y: (e.clientY - rect.top) * scaleY
    };
  }

  function onCanvasMouseDown(e) {
    if (!state.box) return;
    var p = canvasPoint(e);
    var d = sourceBoxToDisplay(state.box);

    var hitCorner = boxCorners(d).find(function (c) {
      return Math.abs(p.x - c.x) <= HANDLE_SIZE && Math.abs(p.y - c.y) <= HANDLE_SIZE;
    });
    if (hitCorner) {
      state.drag = { mode: 'resize', corner: hitCorner.name, start: p, box: Object.assign({}, state.box) };
      e.preventDefault();
      return;
    }

    if (p.x >= d.x && p.x <= d.x + d.w && p.y >= d.y && p.y <= d.y + d.h) {
      state.drag = { mode: 'move', start: p, box: Object.assign({}, state.box) };
      e.preventDefault();
    }
  }

  function onCanvasMouseMove(e) {
    if (!state.drag) return;
    var p = canvasPoint(e);
    var dx = (p.x - state.drag.start.x) * state.scale;
    var dy = (p.y - state.drag.start.y) * state.scale;
    var orig = state.drag.box;

    if (state.drag.mode === 'move') {
      setBox({ x: orig.x + dx, y: orig.y + dy, w: orig.w, h: orig.h }, true);
      return;
    }

    var x = orig.x, y = orig.y, w = orig.w, h = orig.h;
    switch (state.drag.corner) {
      case 'tl':
        x = orig.x + dx; y = orig.y + dy;
        w = orig.w - dx; h = orig.h - dy;
        break;
      case 'tr':
        y = orig.y + dy;
        w = orig.w + dx; h = orig.h - dy;
        break;
      case 'bl':
        x = orig.x + dx;
        w = orig.w - dx; h = orig.h + dy;
        break;
      case 'br':
        w = orig.w + dx; h = orig.h + dy;
        break;
      default:
        break;
    }
    setBox({ x: x, y: y, w: w, h: h }, true);
  }

  function onCanvasMouseUp() {
    state.drag = null;
  }

  // ---------------------------------------------------------------------
  // local file browser (modal, reused for video / image / output-folder pick)
  // ---------------------------------------------------------------------
  function basename(path) {
    return path.split(/[\\/]/).pop() || path;
  }

  function onVideoBrowseClick() {
    openBrowseModal('video', function (path) {
      addOptionAndSelect(els['video-select'], basename(path), path);
      state.videos.push({ name: basename(path), path: path });
      selectVideo(path);
    });
  }

  function onImageBrowseClick() {
    openBrowseModal('image', function (path) {
      addOptionAndSelect(els['image-select'], basename(path), path);
      state.images.push({ name: basename(path), path: path });
      state.selectedImage = path;
    });
  }

  function onOutputDirBrowseClick() {
    openBrowseModal('dir', function (path) {
      els['output-dir-input'].value = path;
    });
  }

  function openBrowseModal(kind, onSelect) {
    state.browse = { kind: kind, dir: '', parent: null, onSelect: onSelect };
    els['browse-modal-select-btn'].classList.toggle('hidden', kind !== 'dir');
    els['browse-modal'].classList.remove('hidden');
    loadBrowseDir('');
  }

  function closeBrowseModal() {
    els['browse-modal'].classList.add('hidden');
    state.browse = { kind: null, dir: '', parent: null, onSelect: null };
  }

  function onBrowseModalKeydown(e) {
    if (e.key === 'Escape' && !els['browse-modal'].classList.contains('hidden')) {
      closeBrowseModal();
    }
  }

  async function loadBrowseDir(dir) {
    try {
      setStatus('Loading folder…');
      var url = '/api/browse?dir=' + encodeURIComponent(dir || '') +
        '&kind=' + encodeURIComponent(state.browse.kind);
      var res = await fetch(url);
      var data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || 'browse failed');
      }
      state.browse.dir = data.dir;
      state.browse.parent = data.parent;
      renderBrowseModal(data);
      setStatus('');
    } catch (err) {
      setStatus('Browse failed: ' + err.message, true);
    }
  }

  function renderBrowseModal(data) {
    els['browse-modal-path'].textContent = data.dir;

    var list = els['browse-modal-list'];
    list.innerHTML = '';

    if (data.parent) {
      list.appendChild(makeBrowseRow('.. (parent folder)', 'dir', function () {
        loadBrowseDir(data.parent);
      }));
    }

    (data.dirs || []).forEach(function (d) {
      list.appendChild(makeBrowseRow(d.name, 'dir', function () {
        loadBrowseDir(d.path);
      }));
    });

    if (state.browse.kind !== 'dir') {
      (data.files || []).forEach(function (f) {
        list.appendChild(makeBrowseRow(f.name, 'file', function () {
          selectBrowseFile(f.path);
        }));
      });
    }
  }

  function makeBrowseRow(label, kindClass, onClick) {
    var row = document.createElement('button');
    row.type = 'button';
    row.className = 'browse-row browse-row-' + kindClass;
    row.textContent = label;
    row.addEventListener('click', onClick);
    return row;
  }

  function selectBrowseFile(path) {
    var onSelect = state.browse.onSelect;
    closeBrowseModal();
    if (onSelect) onSelect(path);
  }

  function onBrowseSelectFolderClick() {
    var onSelect = state.browse.onSelect;
    var dir = state.browse.dir;
    closeBrowseModal();
    if (onSelect) onSelect(dir);
  }

  // ---------------------------------------------------------------------
  // process
  // ---------------------------------------------------------------------
  async function onProcessClick() {
    if (!state.selectedVideo) {
      setStatus('Select a video first', true);
      return;
    }
    if (!state.selectedImage) {
      setStatus('Select a replacement image first', true);
      return;
    }

    var useAuto = els['auto-detect-checkbox'].checked;
    var scale = Number(els['scale-input'].value);
    if (!isFinite(scale) || scale <= 0) scale = 1.5;
    var outputName = els['output-name-input'].value.trim();
    var outputDir = els['output-dir-input'].value.trim();
    var box = useAuto ? null : state.box;
    var fit = state.sizingMode === 'fit';

    els['process-btn'].disabled = true;
    els['busy-indicator'].classList.remove('hidden');
    setStatus('Processing…');

    try {
      var res = await fetch('/api/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video: state.selectedVideo,
          image: state.selectedImage,
          box: box,
          scale: scale,
          fit: fit,
          output_name: outputName,
          output_dir: outputDir
        })
      });
      var data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || 'process failed');
      }
      showResult(data.output);
      setStatus('Done — ' + data.frames + ' frames @ ' + data.fps + ' fps');
    } catch (err) {
      setStatus('Process failed: ' + err.message, true);
    } finally {
      els['process-btn'].disabled = false;
      els['busy-indicator'].classList.add('hidden');
    }
  }

  function showResult(outputPath) {
    var url = '/media?path=' + encodeURIComponent(outputPath);
    els['result-video'].src = url;
    els['download-link'].href = url;
    var fileName = outputPath.split('/').pop();
    els['download-link'].setAttribute('download', fileName || '');
    els['result-panel'].classList.remove('hidden');
  }

  document.addEventListener('DOMContentLoaded', init);
})();
