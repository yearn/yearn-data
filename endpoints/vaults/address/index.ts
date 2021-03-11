import { getAddress, isAddress } from "@ethersproject/address";
import { CachedVault } from "lib/interfaces/vaults";

import { DDBVaultsCache } from "../../../settings/env";
import { get } from "../../../utils/ddb";
import wrap, { LambdaError } from "../../../utils/wrap";

export const handler = wrap(async (event: any) => {
  const input = event.pathParameters.address;
  if (!isAddress(input)) {
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
});
