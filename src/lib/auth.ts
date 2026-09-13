import { NextRequest } from "next/server";

/**
 * Must return the authenticated application's internal user ID.
 * Never trust a userId supplied in the request body.
 */
export async function getAuthenticatedUserId(
  _request: NextRequest,
): Promise<string | null> {
  // Example with Auth.js:
  // const session = await auth();
  // return session?.user?.id ?? null;

  return "your-test-user-id"; // Replace before deployment.
}
