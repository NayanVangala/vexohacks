/**
 * Follow-Through executor. A Google Apps Script web app that really creates calendar events and sends email.
 * It runs as the Google account that deploys it, so deploy it from srijansaha.work@gmail.com.
 *
 * Two locks: a shared secret (TOKEN) and the recipient. Mail goes to DEMO_TO unless the request names an address that
 * is on ALLOWED_TO (comma-separated, Script properties), so a typo or a hallucinated address can never be emailed.
 * Set TOKEN, DEMO_TO and ALLOWED_TO under Project Settings > Script properties. The reply says who it really sent to.
 */

function doPost(e) {
  try {
    var props = PropertiesService.getScriptProperties();
    var req = JSON.parse(e.postData.contents);
    if (!props.getProperty('TOKEN') || req.token !== props.getProperty('TOKEN')) {
      return out({ ok: false, error: 'bad token' });
    }
    if (req.action === 'email') {
      var demo = props.getProperty('DEMO_TO');
      if (!demo) return out({ ok: false, error: 'DEMO_TO is not set' });
      var allowed = (props.getProperty('ALLOWED_TO') || '').toLowerCase().split(',')
        .map(function (a) { return a.trim(); }).filter(String);
      var want = String(req.to || '').trim().toLowerCase();
      var to = want && allowed.indexOf(want) !== -1 ? want : demo;
      GmailApp.sendEmail(to, req.subject || req.title, req.body || '');
      return out({ ok: true, did: 'Email sent to ' + to, to: to });
    }
    if (req.action === 'calendar') {
      var start = new Date(req.start), end = new Date(req.end);
      if (isNaN(start) || isNaN(end)) return out({ ok: false, error: 'bad start or end time' });
      CalendarApp.getDefaultCalendar().createEvent(req.title, start, end, { description: req.details || '' });
      return out({ ok: true, did: 'Calendar event created: ' + req.title });
    }
    return out({ ok: false, error: 'unknown action ' + req.action });
  } catch (err) {
    return out({ ok: false, error: String(err) });
  }
}

function out(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

/** Run this once from the editor so Google asks you to grant Gmail and Calendar access. It sends nothing. */
function authorize() {
  CalendarApp.getDefaultCalendar().getName();
  GmailApp.getInboxUnreadCount();
}
