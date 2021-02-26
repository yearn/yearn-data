import { RegistryV2Contract__factory } from "@contracts/index";
import { Context } from "@data/context";

export const Registry = "v2.registry.ychad.eth";

export async function fetchAddresses(ctx: Context): Promise<string[]> {
  const registry = RegistryV2Contract__factory.connect(Registry, ctx.provider);
  const prodFilter = registry.filters.NewVault(null, null, null, null);
  const prod = await registry.queryFilter(prodFilter);
  return prod.map((event) => event.args && event.args.vault);
}

export async function fetchAddressesExperimental(
  ctx: Context
): Promise<string[]> {
  const registry = RegistryV2Contract__factory.connect(Registry, ctx.provider);
  const testFilter = registry.filters.NewExperimentalVault(
    null,
    null,
    null,
    null
  );
  const test = await registry.queryFilter(testFilter);
  const prod = await fetchAddresses(ctx);
  return test
    .map((event) => event.args && event.args.vault)
    .filter((address) => !prod.includes(address));
}
