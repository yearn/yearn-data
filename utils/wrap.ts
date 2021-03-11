export class LambdaError extends Error {
  name = "LambdaError";
  statusCode: number;
  constructor(message?: string, statusCode?: number) {
    super(message ?? "Unexpected error.");
    this.statusCode = statusCode ?? 500;
  }
}

// Handler wrapper for AWS lambda functions
export default function wrap(
  lambda: (event: unknown, context: unknown, callback: unknown) => unknown
) {
  return async function (
    event: unknown,
    context: unknown,
    callback: unknown
  ): Promise<unknown> {
    let body: unknown;
    let statusCode: number;

    // catch any errors thrown in execution ..
    try {
      body = await lambda(event, context, callback);
      statusCode = 200;
    } catch (error) {
      // .. and format them in an HTTP-friendly way
      console.error(error);
      body = { error: error.message || "Unexpected error." };
      statusCode = error.statusCode || 500;
    }

    return {
      statusCode,
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Credentials": true,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    };
  };
}
