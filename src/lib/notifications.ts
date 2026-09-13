import { ContactChannel, CrisisType, TrustedContact } from "@prisma/client";
import twilio from "twilio";
import { Resend } from "resend";

const twilioClient = twilio(
  process.env.TWILIO_ACCOUNT_SID,
  process.env.TWILIO_AUTH_TOKEN,
);

const resend = new Resend(process.env.RESEND_API_KEY);

type EmergencyAlertInput = {
  recipient: TrustedContact;
  userName: string;
  crisisType: CrisisType;
  latitude: string;
  longitude: string;
  lastPingAt: Date;
};

export async function sendEmergencyAlert({
  recipient,
  userName,
  crisisType,
  latitude,
  longitude,
  lastPingAt,
}: EmergencyAlertInput) {
  const mapsUrl = `https://www.google.com/maps?q=${latitude},${longitude}`;
  const message =
    `EMERGENCY: ${userName} may be unresponsive during a ${crisisType} crisis. ` +
    `Their last location was recorded at ${lastPingAt.toISOString()}: ${mapsUrl}. ` +
    `Please attempt contact and notify local emergency services if appropriate.`;

  const requests: Promise<unknown>[] = [];

  if (
    (recipient.channel === ContactChannel.SMS ||
      recipient.channel === ContactChannel.BOTH) &&
    recipient.phoneNumber
  ) {
    requests.push(
      twilioClient.messages.create({
        body: message,
        from: process.env.TWILIO_FROM_NUMBER,
        to: recipient.phoneNumber,
      }),
    );
  }

  if (
    (recipient.channel === ContactChannel.EMAIL ||
      recipient.channel === ContactChannel.BOTH) &&
    recipient.email
  ) {
    requests.push(
      resend.emails.send({
        from: process.env.ALERT_FROM_EMAIL!,
        to: recipient.email,
        subject: `Emergency safety alert for ${userName}`,
        text: message,
        html: `
          <h2>Emergency safety alert</h2>
          <p><strong>${userName}</strong> may be unresponsive during a
          <strong>${crisisType}</strong> crisis.</p>
          <p>Last ping: ${lastPingAt.toISOString()}</p>
          <p><a href="${mapsUrl}">Open last known location in Maps</a></p>
        `,
      }),
    );
  }

  // Individual failures are retained for logging but do not prevent another
  // contact or channel from receiving their notification.
  return Promise.allSettled(requests);
}