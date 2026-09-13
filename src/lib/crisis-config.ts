import { CrisisType } from "@prisma/client";

export type SafetyTip = {
  title: string;
  instruction: string;
};

export const CRISIS_CONFIG: Record<
  CrisisType,
  {
    label: string;
    safeHavenQuery: string;
    warning: string;
    tips: SafetyTip[];
  }
> = {
  EARTHQUAKE: {
    label: "Earthquake",
    safeHavenQuery: "emergency shelter",
    warning: "Avoid damaged buildings, bridges, power lines, and coastal areas.",
    tips: [
      { title: "Drop, Cover, Hold", instruction: "Get low, cover your head and neck, and hold onto sturdy shelter." },
      { title: "After shaking", instruction: "Move to an open area only when it is safe. Expect aftershocks." },
      { title: "Do not use lifts", instruction: "Use stairs only if the structure appears safe." },
    ],
  },
  FLOOD: {
    label: "Flood",
    safeHavenQuery: "emergency shelter",
    warning: "Never enter floodwater or drive through a flooded road.",
    tips: [
      { title: "Move upward", instruction: "Go to higher ground or an official evacuation shelter immediately." },
      { title: "Avoid water", instruction: "Floodwater may conceal currents, debris, sewage, and live electrical wires." },
      { title: "Follow authorities", instruction: "Use official evacuation routes and alerts over informal shortcuts." },
    ],
  },
  RIOT: {
    label: "Riot / Civil Unrest",
    safeHavenQuery: "police station",
    warning: "Do not approach crowds, confrontations, or police activity.",
    tips: [
      { title: "Leave early", instruction: "Move away from the area using calm, well-lit routes." },
      { title: "Stay neutral", instruction: "Do not film, argue, or display political symbols near a confrontation." },
      { title: "Seek verified help", instruction: "Go to a police station, hospital, or another official safe location." },
    ],
  },
  FIRE: {
    label: "Fire",
    safeHavenQuery: "emergency shelter",
    warning: "Smoke is dangerous. Leave immediately if authorities advise evacuation.",
    tips: [
      { title: "Evacuate", instruction: "Use stairs, close doors behind you, and do not return for belongings." },
      { title: "Stay low", instruction: "If smoke is present, stay close to the floor and cover your nose and mouth." },
      { title: "Call emergency services", instruction: "Call local emergency services from a safe location." },
    ],
  },
  MEDICAL: {
    label: "Medical Emergency",
    safeHavenQuery: "hospital",
    warning: "For life-threatening emergencies, contact local emergency services immediately.",
    tips: [
      { title: "Call for help", instruction: "Call emergency services before travelling when symptoms are severe." },
      { title: "Share location", instruction: "Tell responders your precise location and any immediate hazards." },
      { title: "Do not drive alone", instruction: "Ask another person or an ambulance to transport you if possible." },
    ],
  },
  OTHER: {
    label: "Other Emergency",
    safeHavenQuery: "emergency shelter",
    warning: "Prioritize official instructions and contact emergency services when needed.",
    tips: [
      { title: "Get safe", instruction: "Move away from the immediate danger." },
      { title: "Stay connected", instruction: "Enable Crisis Mode and notify trusted contacts." },
      { title: "Verify information", instruction: "Rely on official government or emergency-management alerts." },
    ],
  },
};