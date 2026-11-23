import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../shared/api";
import { COMMAND_FORMS } from "./CommandForm";
import type { Route } from "./pages";

export type Command = { name: string; usage: string };
type NationalField = { label: string; default: string; required: boolean; long: boolean };
export type NationalCommand = { name: string; fields: NationalField[] };
export type HelpPage = { label: string; description: string; fields: { name: string; value: string }[] };
export type BotJob = { job_id: number; kind: string; actor_id: string; payload: string; status: "pending" | "running" | "done" | "failed"; result: string | null; created_epoch: number; updated_epoch: number };
export type CommandResult = { id: number; command: string; arguments: string; message: string; success: boolean; time: number; national: boolean };
type Scope = "admin" | "national" | "help";

export const NATIONAL = "국가 명의", HELP = "도움말";
export const categoryOf = (name: string) => COMMAND_FORMS[name]?.group ?? "기타";
const scopeOf = (section: string): Scope => section === NATIONAL ? "national" : section === HELP ? "help" : "admin";

/** State of the "관리자 명령어" page: command lists, the open category, the national-account form and the result log. */
export function useCommandTab(initial: Route) {
  const initialScope = initial.page === "commands" ? scopeOf(initial.sub) : "admin";
  const [commands, setCommands] = useState<Command[]>([]);
  const [nationalCommands, setNationalCommands] = useState<NationalCommand[]>([]);
  const [discordOnly, setDiscordOnly] = useState<string[]>([]);
  const [scope, setScope] = useState<Scope>(initialScope);
  const [category, setCategory] = useState(initialScope === "admin" && initial.page === "commands" ? initial.sub : "");
  const [helpPages, setHelpPages] = useState<HelpPage[]>([]);
  const [helpPage, setHelpPage] = useState(0);
  const [jobs, setJobs] = useState<BotJob[]>([]);
  const [nationalCommand, setNationalCommand] = useState("");
  const [nationalValues, setNationalValues] = useState<string[]>([]);
  const [creationHelp, setCreationHelp] = useState("");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [results, setResults] = useState<CommandResult[]>([]);
  const resultSeq = useRef(0);

  // Read through refs so that these counts changing does not recreate load (and refetch the page).
  const commandCount = useRef(0);
  const helpCount = useRef(0);
  commandCount.current = commands.length;
  helpCount.current = helpPages.length;

  const categories = Array.from(new Set(commands.map((item) => categoryOf(item.name))));
  const activeCategory = categories.includes(category) ? category : categories[0] ?? "";
  const visibleCommands = commands.filter((item) => categoryOf(item.name) === activeCategory);
  const section = scope === "national" ? NATIONAL : scope === "help" ? HELP : activeCategory;
  const selectedCommand = commands.find((item) => item.name === command);
  const selectedNational = nationalCommands.find((item) => item.name === nationalCommand);

  // A command outside the open category (e.g. opened from another page) moves to its category.
  useEffect(() => {
    if (!commands.length) return;
    if (!command) setCommand(visibleCommands[0]?.name ?? "");
    else if (scope === "admin" && categoryOf(command) !== activeCategory) setCategory(categoryOf(command));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command, commands.length]);
  useEffect(() => { setNationalValues(selectedNational?.fields.map((field) => field.default) ?? []); }, [selectedNational]);

  const load = useCallback(async () => {
    if (!commandCount.current) {
      const result = await api<{ commands: Command[]; national: NationalCommand[]; discordOnly: string[]; creationHelp: string }>("/api/admin/commands");
      setCommands(result.commands);
      setNationalCommands(result.national);
      setDiscordOnly(result.discordOnly);
      setCreationHelp(result.creationHelp);
      setNationalCommand((current) => current || result.national[0]?.name || "");
    }
    if (scope === "help" && !helpCount.current) setHelpPages((await api<{ pages: HelpPage[] }>("/api/admin/help")).pages);
    if (scope === "national") setJobs((await api<{ jobs: BotJob[] }>("/api/admin/bot-jobs")).jobs);
  }, [scope]);

  function openCategory(next: string) {
    setScope(scopeOf(next));
    if (scopeOf(next) !== "admin") return;
    setCategory(next);
    if (categoryOf(command) !== next) { setCommand(commands.find((item) => categoryOf(item.name) === next)?.name ?? ""); setArgs(""); }
  }
  const openCommand = useCallback((name: string, commandArgs: string, national = false) => {
    if (national) {
      setScope("national");
      setNationalCommand(name);
    } else {
      setScope("admin");
      setCommand(name);
      setArgs(commandArgs);
    }
  }, []);
  const chooseCommand = useCallback((name: string) => { setCommand(name); setArgs(""); }, []);
  /** Back/forward to an address like #commands/거래소. */
  const restoreRoute = useCallback((route: Route) => {
    setScope(scopeOf(route.sub));
    if (route.sub && scopeOf(route.sub) === "admin") setCategory(route.sub);
  }, []);
  const addResult = useCallback((entry: Omit<CommandResult, "id" | "time">) => {
    setResults((items) => [{ ...entry, id: ++resultSeq.current, time: Date.now() / 1000 }, ...items].slice(0, 20));
  }, []);
  const reset = useCallback(() => {
    setCommands([]); setNationalCommands([]); setDiscordOnly([]); setHelpPages([]); setJobs([]);
    setCommand(""); setArgs(""); setResults([]); setNationalCommand(""); setNationalValues([]);
  }, []);

  return {
    commands, nationalCommands, discordOnly, scope, helpPages, helpPage, setHelpPage, jobs, creationHelp,
    command, args, setArgs, results, nationalCommand, setNationalCommand, nationalValues, setNationalValues,
    categories, activeCategory, visibleCommands, section, selectedCommand, selectedNational,
    load, openCategory, openCommand, chooseCommand, restoreRoute, addResult, reset,
  };
}
export type CommandTab = ReturnType<typeof useCommandTab>;
