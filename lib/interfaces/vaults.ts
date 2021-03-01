import { Apy, common, yearn } from "..";

export type FetchedVault =
  | yearn.vault.VaultV1
  | (yearn.vault.VaultV2 & {
      endorsed: boolean;
    });

export type Vault = FetchedVault & {
  apy: Apy | null;
};

export type CachedToken = WithAssets<common.Token>;

export type CachedVault = WithAssets<Vault> & {
  token: CachedToken;
  updated: number;
  tvl: yearn.vault.tvl.Tvl | null;
};

export type WithAssets<T> = T & {
  displayName: string;
  icon: string | null;
};
