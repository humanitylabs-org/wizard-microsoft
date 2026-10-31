// Last page of the Quick connect popup: close it shortly after it says Connected.
setTimeout(() => { try { window.close(); } catch (_) { /* the opener closes it */ } }, 1200);
