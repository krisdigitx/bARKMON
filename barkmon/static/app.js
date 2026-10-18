const $ = id => document.getElementById(id);
let offset = 0, total = 0, zone = 'Europe/London', loading = false, refreshAgain = false;
const size = 100;
const gib = bytes => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(2)} MiB` : `${(bytes / 1024).toFixed(1)} KiB`;
const age = seconds => seconds === null ? '—' : seconds < 60 ? `${Math.floor(seconds)}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${(seconds / 3600).toFixed(1)}h`;
const duration = seconds => seconds >= 60 ? `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s` : `${seconds.toFixed(1)}s`;
const stamp = seconds => new Date(seconds * 1000).toLocaleString('en-GB', {timeZone: zone, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
const params = () => new URLSearchParams({camera: $('camera').value, mode: $('mode').value, ...($('date').value ? {date: $('date').value} : {})});
async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`Request failed (${response.status}). Please retry.`);
  return response.json();
}
function showError(error) { $('error').textContent = error.message; $('error').hidden = false; }
async function refresh() {
  if (loading) { refreshAgain = true; return; }
  loading = true;
  try {
    const cameraList = await request('/api/cameras');
    const selectedCamera = $('camera').value;
    const cameraSignature = JSON.stringify(cameraList.cameras.map(camera => [camera.id, camera.name, camera.host]));
    if ($('camera').dataset.signature !== cameraSignature) {
      $('camera').replaceChildren(...cameraList.cameras.map(camera => new Option(`${camera.name} · ${camera.host}`, camera.id)));
      if (cameraList.cameras.some(camera => camera.id === selectedCamera)) $('camera').value = selectedCamera;
      $('camera').dataset.signature = cameraSignature;
    }
    const camera = cameraList.cameras.find(camera => camera.id === $('camera').value);
    const query = params();
    const [status, summary, list, pipeline] = await Promise.all([
      request(`/api/status?camera=${encodeURIComponent(camera.id)}`), request(`/api/summary?${query}`),
      request(`/api/events?${query}&limit=${size}&offset=${offset}`), request(`/api/pipeline?camera=${encodeURIComponent(camera.id)}`)
    ]);
    if (query.toString() !== params().toString()) { refreshAgain = true; return; }
    zone = status.timezone;
    $('error').hidden = true;
    $('camera-host').textContent = `${camera.name} · ${camera.host}`;
    const detail = status.services.connection_detail;
    $('connection-detail').textContent = detail?.value || 'Waiting for camera worker diagnostics';
    $('connection-updated').textContent = detail ? `Last check ${age(Math.max(0, status.now - detail.updated))} ago. Reconnecting briefly interrupts this camera's capture.` : 'Use Check / reconnect to retry this camera.';
    $('live').textContent = status.recording ? '● Recording' : status.worker_healthy ? '○ Camera disconnected' : '○ Worker offline';
    $('episodes').textContent = summary.episodes;
    $('duration').textContent = duration(summary.barking_seconds);
    $('longest').textContent = duration(summary.longest_episode_seconds);
    $('coverage').textContent = `${summary.coverage_percent}%`;
    $('analysis').textContent = `${summary.analysis_percent}% analysed · ${duration(summary.gap_seconds)} unrecorded`;
    $('period').textContent = `${stamp(summary.start)} — ${stamp(summary.end)} · ${zone}`;
    $('backlog').textContent = `${summary.pending_segments} pending · ${summary.failed_segments} failed`;
    $('storage').textContent = `${(status.free_bytes / 1024 ** 3).toFixed(1)} GiB free · ${status.retention_days}-day retention · Night ${status.night_start}:00–${status.night_end}:00`;
    $('pipeline-format').textContent = pipeline.format;
    $('audio-hours').textContent = `${(pipeline.audio_seconds / 3600).toFixed(2)} h`;
    $('file-count').textContent = `${pipeline.files.toLocaleString()} WAV ${pipeline.files === 1 ? 'file' : 'files'} · ${pipeline.segment_seconds}s target length`;
    $('audio-budget').textContent = `${gib(pipeline.audio_bytes)} / ${gib(pipeline.audio_budget_bytes)}`;
    $('budget-meter').value = Math.min(100, pipeline.audio_budget_percent);
    $('volume-space').textContent = `${gib(pipeline.volume_used_bytes)} / ${gib(pipeline.volume_total_bytes)} volume used · ${gib(pipeline.volume_free_bytes)} free · ${gib(pipeline.database_bytes)} database`;
    $('processed-percent').textContent = `${pipeline.processed_percent}%`;
    $('processing-counts').textContent = `${pipeline.processed_files} processed · ${pipeline.failed_files} failed`;
    const timing = pipeline.services.last_processing_seconds;
    $('processing-time').textContent = timing ? `Last file analysed in ${Number(timing.value).toFixed(2)}s · ${age(Math.max(0, status.now - timing.updated))} ago` : 'Processing time available after the next analysis';
    $('pending-count').textContent = `${pipeline.pending_files} files`;
    $('pending-age').textContent = pipeline.pending_files ? `${duration(pipeline.pending_seconds)} audio · oldest ${age(pipeline.oldest_pending_age_seconds)}` : 'Analysis queue is clear';
    $('latest-recording').textContent = pipeline.latest_recording === null ? 'No recordings yet' : `${stamp(pipeline.latest_recording)} · ${age(pipeline.latest_recording_age_seconds)} ago`;
    $('oldest-recording').textContent = pipeline.oldest_recording === null ? '—' : stamp(pipeline.oldest_recording);
    $('review-counts').textContent = `${pipeline.confirmed} confirmed · ${pipeline.rejected} false ${pipeline.rejected === 1 ? 'alarm' : 'alarms'} · ${pipeline.unreviewed} unreviewed`;
    const services = pipeline.services;
    $('pipeline-health').textContent = `${services.recorder?.value || 'not started'} / ${services.detector?.value || 'not started'}${status.worker_healthy ? '' : ' · worker offline'}`;
    $('retention-explanation').textContent = `Older audio and matching records are removed after ${pipeline.retention_days} days or when the ${gib(pipeline.audio_budget_bytes)} audio budget or free-space reserve is reached, whichever comes first.`;
    $('csv').href = `/api/report.csv?${query}`;
    $('pdf').href = `/api/report.pdf?${query}`;
    $('chart').replaceChildren();
    const max = Math.max(1, ...summary.hourly.map(hour => hour.seconds));
    for (const hour of summary.hourly) {
      const bar = document.createElement('div');
      bar.className = `bar${hour.seconds ? '' : ' empty'}`;
      bar.style.height = `${Math.max(2, hour.seconds / max * 100)}%`;
      bar.title = `${stamp(hour.start)}: ${duration(hour.seconds)}`;
      $('chart').append(bar);
    }
    $('chart-start').textContent = stamp(summary.start);
    $('chart-end').textContent = stamp(summary.end);
    $('events').replaceChildren();
    total = list.total;
    $('event-count').textContent = `${total} detections · ${summary.reviewed} reviewed`;
    for (const event of list.events) {
      const row = document.createElement('tr');
      for (const text of [stamp(event.start), duration(event.end - event.start), event.score.toFixed(2)]) {
        const cell = document.createElement('td'); cell.textContent = text; row.append(cell);
      }
      const reviewCell = document.createElement('td');
      const select = document.createElement('select');
      select.setAttribute('aria-label', `Review detection at ${stamp(event.start)}`);
      for (const [value, label] of [['unreviewed', 'Unreviewed'], ['confirmed', 'Confirmed'], ['rejected', 'False alarm']]) {
        const option = new Option(label, value); select.add(option);
      }
      select.value = event.review;
      select.onchange = async () => {
        select.disabled = true;
        try {
          await request(`/api/events/${event.id}?camera=${encodeURIComponent(camera.id)}`, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({review: select.value})});
          await refresh();
        } catch (error) { select.value = event.review; showError(error); }
        finally { select.disabled = false; }
      };
      reviewCell.append(select); row.append(reviewCell);
      const playCell = document.createElement('td'), play = document.createElement('button');
      play.textContent = '▶ Listen';
      play.onclick = () => {
        const player = $('player'); player.hidden = false;
        const from = Math.max(0, event.start - event.segment_start - 1);
        const to = event.end - event.segment_start + 1;
        player.src = `/api/audio/${event.id}?camera=${encodeURIComponent(camera.id)}#t=${from},${to}`;
        player.play().catch(showError);
      };
      playCell.append(play); row.append(playCell); $('events').append(row);
    }
    if (!list.events.length) {
      const row = document.createElement('tr'), cell = document.createElement('td');
      cell.colSpan = 5; cell.className = 'empty-state';
      cell.textContent = summary.recorded_seconds ? 'No detections in this period. Check analysis coverage above.' : 'No recordings yet for this period. Check the camera and worker status.';
      row.append(cell); $('events').append(row);
    }
    $('previous').disabled = offset === 0;
    $('next').disabled = offset + size >= total;
    $('page').textContent = total ? `${offset + 1}–${Math.min(offset + size, total)} of ${total}` : '0 detections';
  } catch (error) { showError(error); $('live').textContent = '○ Connection unavailable'; }
  finally { loading = false; if (refreshAgain) { refreshAgain = false; refresh(); } }
}
$('refresh').onclick = () => refresh();
for (const id of ['date', 'mode', 'camera']) $(id).onchange = () => { offset = 0; if (id === 'camera') { $('events').replaceChildren(); $('player').pause(); $('player').removeAttribute('src'); $('player').hidden = true; } refresh(); };
$('previous').onclick = () => { offset = Math.max(0, offset - size); refresh(); };
$('next').onclick = () => { offset += size; refresh(); };
$('check-camera').onclick = async () => {
  $('check-camera').disabled = true;
  try {
    await request(`/api/camera/check?camera=${encodeURIComponent($('camera').value)}`, {method: 'POST'});
    $('connection-detail').textContent = 'Check requested. The worker will reconnect shortly; this panel refreshes every 15 seconds.';
  } catch (error) { showError(error); }
  finally { $('check-camera').disabled = false; }
};
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
