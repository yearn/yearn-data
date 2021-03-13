import { handler } from "../endpoints/welcome";

test("index", async () => {
  const event = "event";
  const context = "context";
  const callback = (_, response) => {
    expect(response.statusCode).toEqual(200);
    expect(typeof response.body).toBe("string");
  };
  await handler(event, context, callback);
});
