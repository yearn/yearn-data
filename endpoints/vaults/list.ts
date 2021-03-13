import { DDBVaultsCache } from "../../settings/env";
import { scan } from "../../utils/ddb";
import wrap, { handlerPath } from "../../utils/wrap";

export const handler = wrap(async () => {
  const cached = await scan(DDBVaultsCache, [
    "address",
    "displayName",
    "token.address",
    "token.displayName",
  ]);
  return cached;
});

export default {
  handler: handlerPath(__dirname, "list.handler"),
  events: [
    {
      http: {
        path: "/vaults",
        method: "get",
        caching: {
          enabled: true,
        },
      },
    },
  ],
};
