/**
 * Follow-Through executor. A Google Apps Script web app that really creates calendar events and sends email.
 * It runs as the Google account that deploys it, so deploy it from srijansaha.work@gmail.com.
 *
 * Two locks: a shared secret (TOKEN) and a fixed email recipient (DEMO_TO). Whatever the request says,
 * mail only goes to DEMO_TO. Set both under Project Settings > Script properties.
 */

function doPost(e) {
  try {
    var props = PropertiesService.getScriptProperties();
    var req = JSON.parse(e.postData.contents);
    if (!props.getProperty('TOKEN') || req.token !== props.getProperty('TOKEN')) {
      return out({ ok: false, error: 'bad token' });
    }
    if (req.action === 'email') {
      var to = props.getProperty('DEMO_TO');
      if (!to) return out({ ok: false, error: 'DEMO_TO is not set' });
      GmailApp.sendEmail(to, req.subject || req.title, req.body || '');
      return out({ ok: true, did: 'Email sent to ' + to });
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
