/**
 * Morning Brief mail sender - Google Apps Script web app.
 *
 * Railway can't send email directly on its free/Hobby plans (SMTP is
 * blocked), so morning_brief.py POSTs the finished email here over HTTPS
 * and this script sends it from your Google Workspace Gmail.
 *
 * SETUP (one time):
 *  1. Go to script.google.com while signed in as the account the brief
 *     should come from, click "New project", delete the sample code and
 *     paste this whole file in. Name the project "Morning Brief Sender".
 *  2. Replace CHANGE-ME below with a long random secret (letters/numbers).
 *  3. Deploy > New deployment > type "Web app":
 *       Execute as: Me
 *       Who has access: Anyone
 *     Click Deploy, approve the permission prompt ("send email as you"),
 *     and copy the Web app URL (ends in /exec).
 *  4. In Railway, on the morning brief service, set:
 *       MAIL_WEBHOOK_URL    = that /exec URL
 *       MAIL_WEBHOOK_SECRET = the same secret as below
 *
 * "Anyone" only means the URL can be called without a Google login -
 * requests without the matching secret are rejected, so keep the secret
 * private. If you change this code later, use Deploy > Manage deployments
 * > edit > New version, so the URL stays the same.
 */
const SECRET = 'CHANGE-ME';

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply({ ok: false, error: 'invalid JSON' });
  }
  if (!SECRET || SECRET === 'CHANGE-ME' || body.secret !== SECRET) {
    return reply({ ok: false, error: 'bad secret' });
  }
  const inlineImages = {};
  Object.keys(body.images || {}).forEach(function (cid) {
    inlineImages[cid] = Utilities.newBlob(Utilities.base64Decode(body.images[cid]), 'image/png', cid + '.png');
  });
  try {
    MailApp.sendEmail({
      to: body.to,
      subject: body.subject,
      body: body.text || '',
      htmlBody: body.html,
      inlineImages: inlineImages,
      name: body.name || 'The Morning Brief',
    });
  } catch (err) {
    return reply({ ok: false, error: String(err) });
  }
  return reply({ ok: true });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
