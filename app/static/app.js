(() => {
  const root = document.querySelector('[data-job-id]');
  if (!root) return;
  const terminal = new Set(['completed', 'failed', 'cancelled']);
  const jobId = root.dataset.jobId;
  const initialStatus = root.dataset.jobStatus;
  if (terminal.has(initialStatus)) return;

  const poll = async () => {
    try {
      const response = await fetch(`/api/searches/${jobId}`, {headers: {'Accept': 'application/json'}});
      if (!response.ok) return;
      const job = await response.json();
      const status = document.getElementById('job-status');
      status.textContent = job.status;
      status.className = `status large ${job.status}`;
      document.getElementById('discovered-urls').textContent = job.discovered_urls;
      document.getElementById('crawled-pages').textContent = job.crawled_pages;
      document.getElementById('contacts-found').textContent = job.contacts_found;
      document.getElementById('duplicates-skipped').textContent = job.duplicates_skipped;
      if (terminal.has(job.status)) window.location.reload();
    } catch (_) {}
  };
  setInterval(poll, 3000);
})();
