import { getAddress, isAddress } from "@ethersproject/address";

import { CachedVault } from "../../lib/interfaces/vaults";
import { DDBVaultsCache } from "../../settings/env";
import { get } from "../../utils/ddb";
import wrap, { handlerPath, LambdaError } from "../../utils/wrap";

export const handler = wrap(
  async (event: { pathParameters: { address: string } }) => {
    const input = event.pathParameters.address;
    if (!input || !isAddress(input)) {
      throw new LambdaError(
        "Bad request: identifier provider is not an ethereum address",
        400
      );
    }

    // address needs to be checksummed
    const address = getAddress(input);

    const cached = await get<CachedVault>(DDBVaultsCache, {
      address,
    });

    if (!cached) {
      throw new LambdaError(
        "Not found: vault with identifier provided does not exist",
        404
      );
    }

    return cached;
  }
);

export default {
  handler: handlerPath(__dirname, "address.handler"),
  events: [
    {
      http: {
        path: "/vaults/{address}",
        method: "get",
        caching: {
          enabled: true,
          cacheKeyParameters: [
            {
              name: "request.path.address",
            },
          ],
        },
      },
    },
  ],
};
