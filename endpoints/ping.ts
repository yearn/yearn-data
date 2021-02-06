import wrap from "../utils/wrap";

export const handler = wrap(async () => {
  return { message: "Pong" };
});
