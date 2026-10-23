const $ = id => document.getElementById(id);
let offset = 0, total = 0, zone = 'Europe/London', loading = false, refreshAgain = false;
const size = 100;
const noteDrafts = new Map();
const gib = bytes => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(2)} MiB` : `${(bytes / 1024).toFixed(1)} KiB`;
const age = seconds => seconds === null ? '—' : seconds < 60 ? `${Math.floor(seconds)}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${(seconds / 3600).toFixed(1)}h`;
const duration = seconds => seconds >= 60 ? `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s` : `${seconds.toFixed(1)}s`;
const level = value => value === null || value === undefined ? 'Unavailable' : `${value.toFixed(1)} dBFS`;
const stamp = seconds => new Date(seconds * 1000).toLocaleString('en-GB', {timeZone: zone, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'});
const params = () => new URLSearchParams({camera: $('camera').value, mode: $('mode').value, ...($('date').value ? {date: $('date').value} : {})});
async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`Request failed (${response.status}). Please retry.`);
  return response.json();
}
function showError(error) { $('error').textContent = error.message; $('error').hidden = false; }
async function refresh() {
  if (document.activeElement?.classList.contains('detection-note')) return;
  if (loading) { refreshAgain = true; return; }
  loading = true;
  try {
    const cameraList = await request('/api/cameras');
    const realCameras = cameraList.cameras.slice();
    cameraList.cameras.unshift({id: 'all', name: 'Both cameras', host: 'combined timeline'});
    const selectedCamera = $('camera').value;
    const cameraSignature = JSON.stringify(cameraList.cameras.map(camera => [camera.id, camera.name, camera.host]));
    if ($('camera').dataset.signature !== cameraSignature) {
      $('camera').replaceChildren(...cameraList.cameras.map(camera => new Option(`${camera.name} · ${camera.host}`, camera.id)));
      if (cameraList.cameras.some(camera => camera.id === selectedCamera)) $('camera').value = selectedCamera;
      $('camera').dataset.signature = cameraSignature;
    }
    const camera = cameraList.cameras.find(camera => camera.id === $('camera').value);
    const query = params();
    const combined = camera.id === 'all';
    const overview = combined ? request('/api/overview') : null;
    const [status, summary, list, pipeline] = await Promise.all([
      combined ? overview.then(value => value.status) : request(`/api/status?camera=${encodeURIComponent(camera.id)}`),
      request(`/api/timeline/summary?${query}`),
      request(`${combined ? '/api/timeline' : '/api/events'}?${query}&limit=${size}&offset=${offset}`),
      combined ? overview.then(value => value.pipeline) : request(`/api/pipeline?camera=${encodeURIComponent(camera.id)}`)
    ]);
    if (document.activeElement?.classList.contains('detection-note')) return;
    if (query.toString() !== params().toString()) { refreshAgain = true; return; }
    zone = status.timezone;
    $('error').hidden = true;
    $('camera-host').textContent = `${camera.name} · ${camera.host}`;
    const detail = status.services.connection_detail;
    $('connection-detail').textContent = combined ? realCameras.map(value => `${value.name}: ${value.detail}`).join(' · ') : detail?.value || 'Waiting for camera worker diagnostics';
    $('check-camera').textContent = combined ? 'Reconnect all cameras' : 'Check / reconnect';
    $('connection-updated').textContent = detail ? `Last check ${age(Math.max(0, status.now - detail.updated))} ago. Reconnecting briefly interrupts this camera's capture.` : 'Use Check / reconnect to retry this camera.';
    $('live').textContent = combined ? `${status.recording ? '●' : '○'} ${status.recording_cameras}/${status.total_cameras} recording` : status.recording ? '● Recording' : status.worker_healthy ? '○ Camera disconnected' : '○ Worker offline';
    $('episodes').textContent = summary.episodes;
    $('bark-pulses').textContent = summary.estimated_barks === null ? 'Unavailable (legacy data)' : summary.estimated_barks;
    $('bark-frequency').textContent = summary.barks_per_minute === null ? '—' : `${summary.barks_per_minute.toFixed(2)} pulses/min`;
    $('peak-level').textContent = level(summary.peak_dbfs);
    $('duplicates').textContent = summary.duplicate_detections;
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
    $('csv').href = `/api/timeline.csv?${query}`;
    $('pdf').href = `/api/report.pdf?${query}`;
    const nightQuery = new URLSearchParams(query); nightQuery.set('mode','night');
    const weekQuery = new URLSearchParams(query); weekQuery.set('mode','week');
    $('night-pdf').href = `/api/report.pdf?${nightQuery}`;
    $('week-pdf').href = `/api/report.pdf?${weekQuery}`;
    const archives = await request('/api/reports');
    const saved = $('saved-reports').value;
    $('saved-reports').replaceChildren(...(archives.reports.length ? archives.reports.map(report => new Option(`${report.name} · generated ${stamp(report.generated)}`, report.url)) : [new Option('No saved reports yet','')]));
    if (archives.reports.some(report => report.url === saved)) $('saved-reports').value=saved;
    $('saved-report-download').hidden = !$('saved-reports').value;
    $('saved-report-download').href = $('saved-reports').value;
    $('saved-report-retention').textContent = ` PDF snapshots retained up to ${archives.retention_days} days; audio retention is shorter.`;

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
      const clips = event.clips || [{camera_id: camera.id, camera_name: camera.name, event_id: event.id, source_segment: event.segment_id, note: event.note, rms_dbfs: event.rms_dbfs, url: `/api/clip/${event.id}?camera=${encodeURIComponent(camera.id)}&segment=${encodeURIComponent(event.segment_id)}`}];
      const span = event.end-event.start;
      const rate = event.barks_per_minute ?? (event.bark_count !== null && event.bark_count !== undefined && span ? event.bark_count*60/span : null);
      const values = [
        `${stamp(event.start)} → ${stamp(event.end)}`,
        `${duration(span)} / ${duration(event.active_seconds ?? span)}`,
        `${event.bark_count ?? 'Unavailable'} / ${rate === null ? '—' : rate.toFixed(1)+' per min'}`,
        `${level(event.rms_dbfs)} / ${level(event.peak_dbfs)}`,
        `${event.strongest_camera_name || (!combined && event.rms_dbfs !== null ? camera.name : 'Unavailable')} · ${event.dedup_method || 'single source'}`
      ];
      for (const value of values) { const cell = document.createElement('td'); cell.textContent=value; row.append(cell); }
      const reviewCell = document.createElement('td');
      const select = document.createElement('select');
      select.setAttribute('aria-label', `Review episode at ${stamp(event.start)}`);
      for (const [value,label] of [['unreviewed','Unreviewed'],['confirmed','Confirmed'],['rejected','False alarm']]) select.add(new Option(label,value));
      select.value=event.review;
      select.onchange=async () => {
        select.disabled=true;
        try {
          await Promise.all(clips.map(clip => request(`/api/events/${clip.event_id}?camera=${encodeURIComponent(clip.camera_id)}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({review:select.value})})));
          await refresh();
        } catch (error) { select.value=event.review;showError(error); }
        finally { select.disabled=false; }
      };
      reviewCell.append(select);row.append(reviewCell);
      const audioCell=document.createElement('td'), source=document.createElement('select');
      source.setAttribute('aria-label',`Audio source for episode at ${stamp(event.start)}`);
      clips.forEach((clip,index)=>source.add(new Option(`${clip.camera_name} · ${level(clip.rms_dbfs)}`,String(index))));
      const strongest=clips.findIndex(clip => clip.camera_id===event.strongest_camera && clip.rms_dbfs===event.rms_dbfs);
      source.value=String(Math.max(0,strongest));
      const play=document.createElement('button');play.textContent='▶ Listen';
      play.onclick=()=>{const player=$('player');player.hidden=false;player.src=clips[Number(source.value)].url;player.play().catch(showError);};
      const download=document.createElement('a');download.textContent='Download WAV';download.download='';
      const updateDownload=()=>{download.href=clips[Number(source.value)].url;};source.onchange=updateDownload;updateDownload();
      audioCell.append(source,play,download);
      const watch=document.createElement('button');watch.textContent='Video from both cameras';
      const videoLinks=document.createElement('div');
      watch.onclick=async()=>{
        watch.disabled=true;videoLinks.replaceChildren();
        try {
          // An episode may contain several detections; each keeps its own footage.
          for (const clip of clips) {
            const result=await request(`/api/events/${clip.event_id}/videos?camera=${encodeURIComponent(clip.camera_id)}&segment=${encodeURIComponent(clip.source_segment)}`);
            for (const video of result.videos) {
              const button=document.createElement('button');
              button.textContent=`${video.camera_name} · ${stamp(clip.start ?? event.start)}${video.available ? '' : ` (${video.reason})`}`;
              button.disabled=!video.available;
              button.onclick=()=>{const player=$('video-player');player.hidden=false;player.src=video.url;player.play().catch(showError);};
              videoLinks.append(button);
            }
          }
        } catch(error) {showError(error);} finally {watch.disabled=false;}
      };
      audioCell.append(watch,videoLinks);row.append(audioCell);
      const notesCell=document.createElement('td');
      for (const clip of clips) {
        const key=JSON.stringify([clip.camera_id,clip.event_id,clip.source_segment]);
        const group=document.createElement('div'), label=document.createElement('label');
        label.textContent=`${clip.camera_name} · ${stamp(clip.start ?? event.start)}`;
        const input=document.createElement('textarea');
        input.className='detection-note';input.rows=3;input.maxLength=4000;
        input.placeholder='Add a note…';
        input.value=noteDrafts.get(key) ?? clip.note ?? '';
        input.setAttribute('aria-label',`Note for ${label.textContent}`);
        const save=document.createElement('button'), status=document.createElement('span');
        save.textContent='Save note';status.setAttribute('role','status');
        input.oninput=()=>{noteDrafts.set(key,input.value);status.textContent='Unsaved';};
        save.onclick=async()=>{
          save.disabled=true;input.disabled=true;status.textContent='Saving…';
          try {
            await request(`/api/events/${clip.event_id}/note?camera=${encodeURIComponent(clip.camera_id)}&segment=${encodeURIComponent(clip.source_segment)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:input.value})});
            noteDrafts.delete(key);status.textContent='Saved';
          } catch(error) {status.textContent='Not saved. Retry.';showError(error);}
          finally {save.disabled=false;input.disabled=false;}
        };
        label.append(input);group.append(label,save,status);notesCell.append(group);
      }
      row.append(notesCell);$('events').append(row);
    }
    if (!list.events.length) {
      const row = document.createElement('tr'), cell = document.createElement('td');
      cell.colSpan = 8; cell.className = 'empty-state';
      cell.textContent = summary.recorded_seconds ? 'No detections in this period. Check analysis coverage above.' : 'No recordings yet for this period. Check the camera and worker status.';
      row.append(cell); $('events').append(row);
    }
    $('previous').disabled = offset === 0;
    $('next').disabled = offset + size >= total;
    $('page').textContent = total ? `${offset + 1}–${Math.min(offset + size, total)} of ${total}` : '0 detections';
  } catch (error) { showError(error); $('live').textContent = '○ Connection unavailable'; }
  finally { loading = false; if (refreshAgain) { refreshAgain = false; refresh(); } }
}
$('saved-reports').onchange = () => { $('saved-report-download').hidden = !$('saved-reports').value; $('saved-report-download').href=$('saved-reports').value; };
$('refresh').onclick = () => refresh();
for (const id of ['date', 'mode', 'camera']) $(id).onchange = () => { offset = 0; if (id === 'camera') { $('events').replaceChildren(); $('player').pause(); $('player').removeAttribute('src'); $('player').hidden = true; } refresh(); };
$('previous').onclick = () => { offset = Math.max(0, offset - size); refresh(); };
$('next').onclick = () => { offset += size; refresh(); };
$('check-camera').onclick = async () => {
  $('check-camera').disabled = true;
  try {
    const selected = $('camera').value;
    const targets = selected === 'all' ? (await request('/api/cameras')).cameras.map(camera => camera.id) : [selected];
    await Promise.all(targets.map(id => request(`/api/camera/check?camera=${encodeURIComponent(id)}`, {method: 'POST'})));
    $('connection-detail').textContent = 'Check requested. The worker will reconnect shortly; this panel refreshes every 15 seconds.';
  } catch (error) { showError(error); }
  finally { $('check-camera').disabled = false; }
};
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);

request('/api/settings/retention').then(policy=>{
  if (![...$('retention-days').options].some(option=>Number(option.value)===policy.days)) $('retention-days').add(new Option(`${policy.days} days`,String(policy.days)));
  $('retention-days').value=String(policy.days);
}).catch(showError);
$('save-retention').onclick=async()=>{
  $('save-retention').disabled=true;
  try {await request('/api/settings/retention',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({days:Number($('retention-days').value)})});$('retention-status').textContent='Saved. Applied by automatic cleanup.';}
  catch(error) {showError(error);} finally {$('save-retention').disabled=false;}
};
