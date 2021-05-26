// Handler wrapper for AWS lambda functions
export default function wrap(lambda: (event: unknown, context: unknown, callback: unknown) => unknown) {
  return async function (event: unknown, context: unknown, callback: unknown): Promise<unknown> {
    let body: unknown;
    let statusCode: number;

    // catch any errors thrown in execution ..
    try {
      body = await lambda(event, context, callback);
      statusCode = 200;
    } catch (e) {
      // .. and format them in an HTTP-friendly way
      console.error(e);
      body = { error: e.message || "Unexpected error." };
      statusCode = e.statusCode || 500;
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
