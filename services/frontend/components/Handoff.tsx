/** A sand handoff block: a heading and a concrete free next step. Used by every state that has to
 * point the reader somewhere else instead of finishing the job itself -- a refusal, a no-answer,
 * or a blocked answer. Never styled as an error; it is a normal, expected part of the product. */
export default function Handoff({
  heading,
  children,
}: {
  heading: string;
  children: React.ReactNode;
}) {
  return (
    <div className="my-6 rounded-md bg-sand px-5 py-[18px]">
      <h3 className="mb-2 text-[15px] font-semibold text-ink">{heading}</h3>
      <p className="m-0 text-[15px] leading-[1.7] text-[#544537]">{children}</p>
    </div>
  );
}
