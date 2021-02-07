import wrap from "../utils/wrap";

export const handler = wrap(async () => {
  return { message: "welcome to the yearn.finance api" };
});
