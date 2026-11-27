import { Children, isValidElement, type ChangeEvent, type ReactElement, type ReactNode } from "react";
import { Select } from "./overlays";

type OptionProps = { value?: string | number; disabled?: boolean; children?: ReactNode; label?: string };

/** Plain text of an option's children (options may be built from several expressions). */
function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return "";
}

function collect(children: ReactNode, group?: string): { value: string; label: string; disabled?: boolean }[] {
  return Children.toArray(children).flatMap((child) => {
    if (!isValidElement(child)) return [];
    const element = child as ReactElement<OptionProps>;
    if (element.type === "optgroup") return collect(element.props.children, element.props.label);
    if (element.type === "option") {
      const label = textOf(element.props.children);
      const value = element.props.value === undefined ? label : String(element.props.value);
      return [{ value, label: group ? `${label}` : label, disabled: element.props.disabled }];
    }
    return collect((element.props as { children?: ReactNode }).children, group);
  });
}

/**
 * Drop-in replacement for a single-value `<select>`: same `value`/`onChange(event)` contract and `<option>`
 * children, rendered with the app's styled dropdown instead of the operating system's list.
 */
export function SelectInput({
  value, onChange, children, disabled, required, name, id, className, title, "aria-label": ariaLabel,
}: {
  value?: string | number;
  onChange?: (event: ChangeEvent<HTMLSelectElement>) => void;
  children?: ReactNode;
  disabled?: boolean;
  required?: boolean;
  name?: string;
  id?: string;
  className?: string;
  title?: string;
  "aria-label"?: string;
}) {
  const options = collect(children);
  const current = value === undefined ? options[0]?.value ?? "" : String(value);
  return (
    <span className="block w-full min-w-0" title={title} id={id}>
      <Select
        aria-label={ariaLabel ?? title}
        value={current}
        disabled={disabled}
        className={className}
        options={options}
        onValueChange={(next) => {
          // Handlers written for a native select read event.target.value (sometimes currentTarget).
          const target = { value: next, name: name ?? "", required: Boolean(required) } as unknown as HTMLSelectElement;
          onChange?.({ target, currentTarget: target } as ChangeEvent<HTMLSelectElement>);
        }}
      />
    </span>
  );
}
