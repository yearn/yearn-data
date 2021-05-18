import { Response } from "node-fetch";

export async function handleHTTPError(response: Response): Promise<Response> {
  if (!response.ok) {
    console.error(response);
    throw Error(response.statusText);
  }
  return response;
}
