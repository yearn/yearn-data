import wrap, { handlerPath } from "../utils/wrap";

export const handler = wrap(async () => {
  return { message: "welcome to the yearn.finance api" };
});

export default {
  handler: handlerPath(__dirname, "welcome.handler"),
  events: [
    {
      http: {
        path: "/",
        method: "get",
        caching: {
          enabled: true,
        },
      },
    },
  ],
};
