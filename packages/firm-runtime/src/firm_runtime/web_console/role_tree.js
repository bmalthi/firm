// Role tree builder for the owner console.
function parentAddress(address) {
  const separator = address.lastIndexOf("/");
  return separator === -1 ? null : address.slice(0, separator);
}

export function buildRoleForest(roles) {
  const nodes = new Map(roles.map((role) => [role.address, {role, children: []}]));
  const roots = [];
  for (const node of nodes.values()) {
    const parent = nodes.get(parentAddress(node.role.address));
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const byAddress = (left, right) => left.role.address.localeCompare(right.role.address);
  const sortBranch = (node) => {
    node.children.sort(byAddress);
    node.children.forEach(sortBranch);
  };
  roots.sort(byAddress);
  roots.forEach(sortBranch);
  return roots;
}
