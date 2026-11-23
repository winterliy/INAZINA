import { useEffect, useState } from "react";
import type { Dispatch, FormEvent, SetStateAction } from "react";
import { api, type ApiUser } from "../../shared/api";
import type { OwnershipGraphData } from "../../shared/OwnershipGraph";
import { errorText, type TaxResidency } from "./appShared";

type Options = {
  user: ApiUser | null;
  pathname: string;
  setNotice: (text: string) => void;
  taxResidencyValue: string;
  setTaxResidency: Dispatch<SetStateAction<TaxResidency | null>>;
};

const OWNERSHIP_PATHS = ["/ownership", "/ownership/"];

/** Login-ID and tax-residency forms of the account, plus the ownership graph shown on the account and ownership pages. */
export function useAccountSettings({ user, pathname, setNotice, taxResidencyValue, setTaxResidency }: Options) {
  const [htsId, setHtsId] = useState("");
  const [htsIdInput, setHtsIdInput] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [settingsMessage, setSettingsMessage] = useState("");
  const [settingsError, setSettingsError] = useState("");
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [taxResidencyOpen, setTaxResidencyOpen] = useState(false);
  const [ownershipGraph, setOwnershipGraph] = useState<OwnershipGraphData>();
  const [ownershipLoading, setOwnershipLoading] = useState(() => OWNERSHIP_PATHS.includes(location.pathname));

  useEffect(() => {
    if (!user) return;
    void api<{ htsId: string | null }>("/api/hts/account")
      .then(({ htsId: current }) => { setHtsId(current ?? ""); setHtsIdInput(current ?? ""); })
      .catch((cause) => setNotice(errorText(cause, "나루 계정 정보를 불러오지 못했습니다.")));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  useEffect(() => {
    if (!user || !["/account", "/account/", ...OWNERSHIP_PATHS].includes(pathname)) return;
    let active = true;
    setOwnershipLoading(true);
    void api<{ graph: OwnershipGraphData }>("/api/me/ownership-graph")
      .then(({ graph }) => { if (active) setOwnershipGraph(graph); })
      .catch(() => { if (active) setOwnershipGraph(undefined); })
      .finally(() => { if (active) setOwnershipLoading(false); });
    return () => { active = false; };
  }, [user, pathname]);

  /** Runs one settings form: clears the messages, shows the success text or the error, and keeps the button busy meanwhile. */
  async function runSettings(action: () => Promise<string>, failed: (cause: unknown) => string) {
    setSettingsBusy(true);
    setSettingsError("");
    setSettingsMessage("");
    try { setSettingsMessage(await action()); } catch (cause) { setSettingsError(failed(cause)); } finally { setSettingsBusy(false); }
  }

  function saveTaxResidency(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    return runSettings(async () => {
      const saved = await api<{ taxResidency: string | null }>("/api/me/tax-residency", {
        method: "POST", body: JSON.stringify({ residency: taxResidencyValue || null }),
      });
      setTaxResidency((current) => current ? { ...current, taxResidency: saved.taxResidency } : current);
      return "세무거주지를 저장했습니다. 이후 확정되는 주주명부부터 적용됩니다.";
    }, (cause) => errorText(cause, "세무거주지를 저장하지 못했습니다."));
  }

  function saveHtsId(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    return runSettings(async () => {
      const result = await api<{ htsId: string }>("/api/hts/account", { method: "POST", body: JSON.stringify({ htsId: htsIdInput, password: currentPassword }) });
      setHtsId(result.htsId);
      setHtsIdInput(result.htsId);
      setCurrentPassword("");
      return "아이디를 저장했습니다.";
    }, (cause) => {
      const reason = errorText(cause, "");
      return reason === "invalid_credentials" ? "현재 비밀번호가 일치하지 않습니다." : reason || "아이디를 저장하지 못했습니다.";
    });
  }

  return {
    htsId, htsIdInput, setHtsIdInput, currentPassword, setCurrentPassword,
    settingsMessage, settingsError, settingsBusy, saveHtsId, saveTaxResidency,
    taxResidencyOpen, setTaxResidencyOpen, ownershipGraph, ownershipLoading,
  };
}
